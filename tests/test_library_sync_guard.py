"""The library sync must not be able to destroy the library.

`library_track` is the only record of what the user has, and `pick_seeds`
reads nothing else -- so losing it takes every mood recommendation with it.
On 2026-09-16 that happened: a run replaced a 1,783-track library with one
malformed row (`video_id='b'`, `title='T'`, `artists='AP Dhillon'` -- the
shape of a test fixture, and of the string-is-iterable bug recommend.py:648
already documents three prior instances of).

Nothing announced it. The atlas was untouched, so `index_status` still
reported 65,438 indexed tracks and 100% library mood coverage -- of a library
of one. Every `recommend_for_mood` call returned zero songs and blamed "the
library exclusion", which sent the next reader looking at the filter instead
of the missing input.

These tests pin the three things that were wrong: the replace was unguarded,
the fetch's failures were invisible, and the shortfall named the wrong cause.
"""

import pytest

import label
import recommend
import store


def _entries(count, playlist="Liked Music"):
    return [(f"v{i}", playlist, True) for i in range(count)]


# --- the replace is guarded ------------------------------------------------


def test_refuses_to_replace_a_populated_library_with_almost_nothing(db):
    """The incident, reduced: many tracks in, one malformed row out."""
    store.sync_library(db, _entries(1783))

    with pytest.raises(store.LibraryShrankError) as excinfo:
        store.sync_library(db, [("b", "Liked Music", True)])

    # The library is still there -- refusing means not performing the DELETE,
    # not performing it and reporting afterwards.
    assert len(store.library_video_ids(db)) == 1783
    assert "1783" in str(excinfo.value) and "force=True" in str(excinfo.value)


def test_a_normal_sync_still_replaces(db):
    """Songs do get unliked; an ordinary shrink must not trip the guard."""
    store.sync_library(db, _entries(100))
    store.sync_library(db, _entries(95))
    assert len(store.library_video_ids(db)) == 95


def test_small_libraries_are_not_held_hostage(db):
    """A ratio is meaningless on tiny libraries, and the existing suite builds
    them constantly -- 2 tracks down to 1 must stay legal."""
    store.sync_library(db, _entries(2))
    store.sync_library(db, [("b", "New", True)])
    assert store.library_video_ids(db) == {"b"}


def test_force_still_allows_a_real_deletion(db):
    """Someone who really did clear their library has to be able to say so."""
    store.sync_library(db, _entries(1783))
    store.sync_library(db, [("b", "Liked Music", True)], force=True)
    assert store.library_video_ids(db) == {"b"}


def test_first_sync_into_an_empty_store_is_never_blocked(db):
    store.sync_library(db, _entries(1783))
    assert len(store.library_video_ids(db)) == 1783


# --- the fetch's failures are visible --------------------------------------


class _FakeYT:
    """A provider whose playlist fetches all fail, as a dead token's would."""

    def __init__(self, liked, playlists, fail=False):
        self._liked, self._playlists, self._fail = liked, playlists, fail

    def get_playlist(self, playlist_id, limit=None):
        if playlist_id == "LM":
            return {"tracks": self._liked}
        if self._fail:
            raise RuntimeError("HTTP 401")
        return {"tracks": [{"videoId": "x1", "title": "X", "artists": "A"}]}

    def get_library_playlists(self, limit=None):
        return self._playlists


def test_sync_reports_how_many_playlists_failed(db):
    """Silently skipping these made total failure indistinguishable from success."""
    yt = _FakeYT(
        liked=[{"videoId": "a", "title": "A", "artists": "Artist"}],
        playlists=[{"playlistId": f"p{i}", "title": f"P{i}"} for i in range(5)],
        fail=True,
    )

    result = label.sync_library(db, yt)

    assert result["failed_playlists"] == 5
    assert result["playlists"] == 0


def test_a_healthy_sync_reports_no_failures(db):
    yt = _FakeYT(
        liked=[{"videoId": "a", "title": "A", "artists": "Artist"}],
        playlists=[{"playlistId": "p1", "title": "P1"}],
    )

    result = label.sync_library(db, yt)

    assert result["failed_playlists"] == 0
    assert result["playlists"] == 1


# --- the shortfall names the right cause -----------------------------------


def test_empty_library_is_reported_as_the_cause_not_the_exclusion(db):
    """The note that cost an hour of debugging. An empty library and an
    over-strict filter are different problems and must read differently."""
    result = recommend.build(
        None, db, set(), feeling="angry", use_history=False, limit=5
    )

    joined = " ".join(result["notes"]).lower()
    assert "refresh_library" in joined
    assert "library exclusion" not in joined


def test_a_barely_populated_library_says_the_sync_probably_failed(db):
    store.sync_library(db, [("b", "Liked Music", True)])

    result = recommend.build(
        None, db, set(), feeling="angry", use_history=False, limit=5
    )

    joined = " ".join(result["notes"]).lower()
    assert "sync failed" in joined or "too few" in joined
