"""Two provider instances must not share one namespace.

Every id re-com persists belongs to exactly one backend and they are not
interchangeable -- an 11-char YouTube videoId can never equal a 22-char
Spotify track id. Before this was enforced, both instances defaulted to the
same store and the same library cache, so the Spotify instance's exclusion
set was 1,499 YouTube ids that could match nothing: the novelty guarantee
silently excluded nothing at all, and the mood tools returned YouTube ids as
Spotify recommendations.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import provider  # noqa: E402
import server  # noqa: E402


# --- provider.active --------------------------------------------------------


def test_provider_defaults_to_youtube(monkeypatch):
    monkeypatch.delenv("RECOM_PROVIDER", raising=False)
    assert provider.active() == "youtube"


def test_provider_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("RECOM_PROVIDER", "Spotify")
    assert provider.active() == "spotify"


# --- provider.scoped_path ---------------------------------------------------


def test_the_default_provider_keeps_the_plain_filename():
    """Existing installs must not wake up to an empty store: the YouTube
    instance keeps the crawled atlas, labels and history it already has."""
    base = Path("/tmp/.recom/store.db")
    assert provider.scoped_path(base, "youtube") == base


def test_another_provider_gets_its_own_file():
    base = Path("/tmp/.recom/store.db")
    assert provider.scoped_path(base, "spotify") == Path("/tmp/.recom/store-spotify.db")


def test_scoping_preserves_multi_part_suffixes():
    base = Path("/tmp/.recom/library_cache.json")
    assert provider.scoped_path(base, "spotify") == Path("/tmp/.recom/library_cache-spotify.json")


def test_two_providers_never_collide():
    base = Path("/tmp/.recom/store.db")
    paths = {provider.scoped_path(base, p) for p in ("youtube", "spotify", "tidal")}
    assert len(paths) == 3


def test_scoped_path_reads_the_environment_when_not_told(monkeypatch):
    monkeypatch.setenv("RECOM_PROVIDER", "spotify")
    assert provider.scoped_path(Path("/tmp/x.db")).name == "x-spotify.db"


# --- the wiring: store and cache actually pick it up ------------------------


@pytest.fixture
def reload_env(tmp_path, monkeypatch):
    """Re-import store/server under a chosen environment, safely.

    These tests exercise the *default* paths, which resolve under the home
    directory -- so HOME is redirected into tmp_path first. Reloading a module
    would otherwise overwrite the conftest fixture's DB_PATH override and point
    the suite at the developer's real store, which conftest exists to prevent.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    def reload(module, **env):
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        return importlib.reload(module)

    yield reload

    # Restore both modules to the environment the rest of the suite expects.
    monkeypatch.setenv("RECOM_PROVIDER", "youtube")
    monkeypatch.delenv("RECOM_DB_PATH", raising=False)
    monkeypatch.delenv("RECOM_CACHE_PATH", raising=False)
    import store

    importlib.reload(store)
    importlib.reload(server)


def test_store_path_is_scoped_to_the_provider(reload_env):
    import store

    youtube_path = reload_env(store, RECOM_PROVIDER="youtube", RECOM_DB_PATH=None).DB_PATH
    spotify_path = reload_env(store, RECOM_PROVIDER="spotify", RECOM_DB_PATH=None).DB_PATH

    assert youtube_path != spotify_path
    assert youtube_path.name == "store.db"
    assert spotify_path.name == "store-spotify.db"


def test_an_explicit_db_path_still_wins(reload_env):
    import store

    reloaded = reload_env(store, RECOM_PROVIDER="spotify", RECOM_DB_PATH="/tmp/explicit.db")
    assert reloaded.DB_PATH == Path("/tmp/explicit.db")


def test_cache_path_is_scoped_to_the_provider(reload_env):
    """The exclusion set is the guarantee this project exists for; sharing it
    across backends is the most damaging collision of the lot."""
    reloaded = reload_env(server, RECOM_PROVIDER="spotify", RECOM_CACHE_PATH=None)
    assert reloaded.CACHE_PATH.name == "library_cache-spotify.json"
    assert reloaded.PROVIDER == "spotify"


def test_the_two_backends_never_share_an_exclusion_set(reload_env):
    """The specific bug this file exists for, stated as an assertion."""
    yt_cache = reload_env(server, RECOM_PROVIDER="youtube", RECOM_CACHE_PATH=None).CACHE_PATH
    sp_cache = reload_env(server, RECOM_PROVIDER="spotify", RECOM_CACHE_PATH=None).CACHE_PATH
    assert yt_cache != sp_cache


# --- the mood tools refuse rather than mislead ------------------------------


@pytest.mark.parametrize("backend", ["youtube", "spotify"])
def test_mood_support_passes_on_a_backend_with_a_mood_index(monkeypatch, backend):
    """Spotify joined this list on measured evidence, not on the graph atlas
    merely existing: 40.2% library mood coverage and a pipeline that returns
    real mood-ranked tracks. See PLAN.md, "The Spotify mood gate"."""
    monkeypatch.setattr(server, "PROVIDER", backend)
    server._require_mood_support()  # must not raise


@pytest.mark.parametrize("backend", ["tidal"])
def test_mood_support_refuses_on_other_backends(monkeypatch, backend):
    monkeypatch.setattr(server, "PROVIDER", backend)
    with pytest.raises(RuntimeError) as excinfo:
        server._require_mood_support()

    message = str(excinfo.value)
    assert backend in message
    # The error has to say what to do instead, not just what failed.
    assert "recommend_from_song" in message


@pytest.mark.parametrize(
    "tool",
    ["recommend_for_mood", "recommend_from_playlist_for_mood", "read_my_mood"],
)
def test_every_mood_tool_is_gated(monkeypatch, tool):
    """Gated before touching the provider or the store -- a wrong-namespace
    call must never be made at all. Checked against a backend with no mood
    index at all, since Spotify now has one."""
    monkeypatch.setattr(server, "PROVIDER", "tidal")

    def _explode(*a, **k):
        raise AssertionError("the provider must not be reached on a gated backend")

    monkeypatch.setattr(server, "_client", _explode)

    kwargs = {"playlist_id": "PL1"} if tool == "recommend_from_playlist_for_mood" else {}
    with pytest.raises(RuntimeError, match="isn't available"):
        getattr(server, tool)(**kwargs)


@pytest.mark.parametrize("tool", ["recommend_from_song", "songs_by_artist"])
def test_v1_tools_are_not_gated(monkeypatch, tool):
    """These work on every backend and must stay reachable."""
    monkeypatch.setattr(server, "PROVIDER", "spotify")

    class _Reached(Exception):
        pass

    def _fake_client():
        raise _Reached

    monkeypatch.setattr(server, "_client", _fake_client)
    kwargs = {"song": "x"} if tool == "recommend_from_song" else {"artist": "x"}
    with pytest.raises(_Reached):
        getattr(server, tool)(**kwargs)


def test_index_status_names_the_backend_it_describes(monkeypatch, db):
    monkeypatch.setattr(server, "PROVIDER", "spotify")
    status = server.index_status()
    assert status["provider"] == "spotify"
    assert status["mood_supported"] is True
    assert "store" in status


def test_index_status_reports_a_backend_without_a_mood_index_honestly(monkeypatch, db):
    monkeypatch.setattr(server, "PROVIDER", "tidal")
    assert server.index_status()["mood_supported"] is False
