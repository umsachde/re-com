import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graph_store  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point every persistent store at a per-test temp path.

    Without this, tests would read and write the developer's real library cache
    and mood store -- polluting them, and letting one test's data silently
    satisfy another test's lookup.

    `graph_store.DB_PATH` is redirected for the same reason and needs it more,
    not less: unlike store.db it is deliberately NOT scoped per provider, so a
    single unredirected test would write into the one graph cache every backend
    shares -- and a cached negative resolution is indistinguishable from a real
    one later.
    """
    monkeypatch.setattr(server, "CACHE_PATH", tmp_path / "library_cache.json")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "store.db")
    monkeypatch.setattr(graph_store, "DB_PATH", tmp_path / "graph.db")
    # server caches these connections across calls; drop them so they reopen
    # against this test's temp paths rather than the previous test's.
    monkeypatch.setattr(server, "_store_conn", None)
    monkeypatch.setattr(server, "_graph_conn_cache", None)
    # The music graph is a live network dependency, so it is off unless a test
    # asks for it (see the `graph_enabled` fixture). Every pre-v6 test states a
    # native-signal expectation and must keep getting native-only behaviour.
    monkeypatch.setattr(server, "GRAPH_ENABLED", False)
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if anything in the suite opens a real socket.

    Both test modules claim "no network" in their docstrings, and that claim
    was silently false the moment v6 wired the Deezer graph into
    `recommend_from_song`: six server tests started calling api.deezer.com for
    real. They still passed, which is exactly why this guard exists -- a test
    that quietly depends on the internet is slow, flaky offline, and no longer
    testing what it says it tests.
    """

    def boom(*args, **kwargs):
        raise AssertionError(
            "This test attempted a real network call. Fake the transport "
            "(graph._get, or the provider) instead."
        )

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


@pytest.fixture
def graph_enabled(monkeypatch, isolated_state):
    """Turn the music graph on for one test, pointed at a temp cache.

    Callers must still fake `graph._get` -- `no_network` will fail the test
    otherwise, which is the intended outcome.
    """
    monkeypatch.setattr(server, "GRAPH_ENABLED", True)
    monkeypatch.setattr(server, "_graph_conn_cache", None)
    return isolated_state / "graph.db"


@pytest.fixture
def isolated_cache(isolated_state):
    return isolated_state / "library_cache.json"


@pytest.fixture
def db(isolated_state):
    """An isolated SQLite store, never the developer's real one."""
    conn = store.connect(isolated_state / "store.db")
    yield conn
    conn.close()


@pytest.fixture
def graph_db(isolated_state):
    """An isolated music-graph cache, never the developer's real one."""
    conn = graph_store.connect(isolated_state / "graph.db")
    yield conn
    conn.close()
