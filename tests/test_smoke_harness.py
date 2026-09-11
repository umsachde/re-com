"""The live smoke harness's own judgement, checked without a live account.

`scripts/smoke_all.py` needs credentials and two running *-mcp servers, so it
cannot run in CI -- which makes it exactly the kind of code that rots into
always-passing without anyone noticing. A smoke test that cannot fail is worse
than no smoke test, because it reads as evidence.

These tests cover only the pure judgement: what counts as a pass, what counts
as a leak, what counts as too slow, and whether an unconfigured backend can be
mistaken for a healthy one. The live half is verified by running it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import smoke_all


def _check(seconds=1.0):
    check = smoke_all.Check("tool")
    check.seconds = seconds
    return check


def _song(video_id, title="Song"):
    return {"videoId": video_id, "title": title}


# --- shapes -----------------------------------------------------------------


def test_songs_are_found_in_both_wrappers():
    # recommend_from_playlist returns a bare list; every other tool returns a
    # dict with "songs". The harness must not silently score one as empty.
    assert smoke_all._songs_of([_song("a")]) == [_song("a")]
    assert smoke_all._songs_of({"songs": [_song("a")]}) == [_song("a")]
    assert smoke_all._songs_of({"songs": []}) == []
    assert smoke_all._songs_of(None) == []


# --- the three invariants ---------------------------------------------------


def test_a_full_clean_result_passes():
    check = smoke_all._check(_check(), {"songs": [_song("a")]}, {"b"}, budget=30)
    assert check.status == smoke_all.PASS


def test_an_empty_result_fails_when_nothing_explains_it():
    # The failure mode this whole script exists to catch: silent emptiness.
    check = smoke_all._check(_check(), {"songs": []}, set(), budget=30)
    assert check.status == smoke_all.FAIL
    assert "no reason" in check.detail


def test_an_empty_result_passes_when_the_tool_says_why():
    # recommend_from_playlist_for_mood is documented to return nothing rather
    # than off-mood filler, so long as it explains itself.
    check = smoke_all._check(
        _check(), {"songs": [], "notes": ["nothing in this playlist fits"]}, set(),
        budget=30, allow_empty_if=smoke_all._stated_shortfall,
    )
    assert check.status == smoke_all.PASS
    assert "nothing in this playlist fits" in check.detail


def test_a_library_song_in_the_results_fails():
    # The guarantee the project exists for. A live run is the only place it can
    # actually be tested, so this must not be forgiving.
    check = smoke_all._check(
        _check(), {"songs": [_song("a"), _song("known", "Already Liked")]}, {"known"},
        budget=30,
    )
    assert check.status == smoke_all.FAIL
    assert "Already Liked" in check.detail


def test_a_correct_but_slow_result_fails():
    check = smoke_all._check(_check(seconds=61.0), {"songs": [_song("a")]}, set(), budget=30)
    assert check.status == smoke_all.FAIL
    assert "budget" in check.detail


def test_the_first_video_id_is_kept_for_explain_and_feedback():
    out = {}
    smoke_all._check(_check(), {"songs": [_song("a"), _song("b")]}, set(), budget=30, out=out)
    assert out["_explain_id"] == "a"


def test_a_crashing_tool_is_a_failed_check_not_a_crashed_run():
    checks = []
    result = smoke_all._run("boom", lambda: 1 / 0, checks)
    assert result is None
    assert checks[0].status == smoke_all.FAIL
    assert "ZeroDivisionError" in checks[0].detail


# --- an unrun check must never read as a green one --------------------------


def test_an_unconfigured_backend_is_not_run(monkeypatch):
    monkeypatch.delenv("RECOM_YTMUSIC_MCP_COMMAND", raising=False)
    monkeypatch.delenv("RECOM_SPOTIFY_MCP_COMMAND", raising=False)
    assert smoke_all.configured_providers("all") == []


def test_only_configured_backends_are_run(monkeypatch):
    monkeypatch.setenv("RECOM_YTMUSIC_MCP_COMMAND", "/usr/bin/python")
    monkeypatch.delenv("RECOM_SPOTIFY_MCP_COMMAND", raising=False)
    assert smoke_all.configured_providers("all") == ["youtube"]


def test_an_explicit_provider_is_honoured_even_if_unconfigured(monkeypatch):
    # Asking for a backend by name should report why it failed, not quietly
    # run nothing and exit 0.
    monkeypatch.delenv("RECOM_SPOTIFY_MCP_COMMAND", raising=False)
    assert smoke_all.configured_providers("spotify") == ["spotify"]


def test_a_backend_that_never_started_is_a_failure(capsys):
    status = smoke_all.report([{"provider": "spotify", "checks": [], "error": "auth expired"}])
    assert status == 1
    assert "auth expired" in capsys.readouterr().out


def test_a_run_with_no_checks_is_a_failure():
    # Zero checks and zero failures must not add up to "OK".
    assert smoke_all.report([{"provider": "youtube", "checks": []}]) == 1


def test_a_failed_check_sets_a_nonzero_exit():
    run = {"provider": "youtube", "checks": [
        {"name": "recommend_from_playlist", "status": smoke_all.FAIL,
         "seconds": 1.0, "detail": "ProgrammingError"},
    ]}
    assert smoke_all.report([run]) == 1


def test_all_passing_checks_exit_zero():
    run = {"provider": "youtube", "checks": [
        {"name": "recommend_from_song", "status": smoke_all.PASS,
         "seconds": 4.3, "detail": "10 songs, none in the library"},
    ]}
    assert smoke_all.report([run]) == 0


def test_skipped_checks_are_reported_and_do_not_fail_the_run(capsys):
    run = {"provider": "spotify", "checks": [
        {"name": "recommend_for_mood", "status": smoke_all.SKIP,
         "seconds": 0.0, "detail": "mood tools are gated off on spotify"},
    ]}
    assert smoke_all.report([run]) == 0
    assert "1 skipped" in capsys.readouterr().out
