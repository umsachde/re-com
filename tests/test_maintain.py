"""`scripts/maintain.py`'s own logic, checked without a live crawl.

The stages themselves (`atlas.crawl`, `tempo.backfill`, `graph_atlas.crawl`,
`brainz.resolve_artist`) are each tested where they live. What belongs here is
`maintain.py`'s own bookkeeping -- which library artists a bounded MusicBrainz
warm-up run (PLAN.md 7.9) should attempt, since getting that wrong either
re-pays the 1.2s throttle on already-cached artists or silently never warms an
artist the live path will hit cold.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import graph_store
import store
import maintain


def _library(conn, *entries):
    """(video_id, artists) pairs, synced together.

    `sync_library` replaces the whole `library_track` table on every call, so
    building the library one artist at a time here would have each call wipe
    the last -- it must be one call with every row.
    """
    store.upsert_tracks(conn, [{"videoId": vid, "title": "T", "artists": artists} for vid, artists in entries])
    store.sync_library(conn, [(vid, "Liked Music", True) for vid, _ in entries])


def test_pending_brainz_artists_finds_uncached_library_artists():
    conn = store.connect()
    graph_conn = graph_store.connect()
    _library(conn, ("a", "Arijit Singh"), ("b", "AP Dhillon"))

    assert maintain._pending_brainz_artists(conn, graph_conn, limit=None) == ["ap dhillon", "arijit singh"]


def test_pending_brainz_artists_skips_already_cached():
    conn = store.connect()
    graph_conn = graph_store.connect()
    _library(conn, ("a", "Arijit Singh"), ("b", "AP Dhillon"))
    graph_store.put_brainz_artist(graph_conn, "arijit singh", mbid="mb-1", name="Arijit Singh", status="ok")

    assert maintain._pending_brainz_artists(conn, graph_conn, limit=None) == ["ap dhillon"]


def test_pending_brainz_artists_skips_cached_negative_results_too():
    """A `no_match` row is still a resolved outcome, not a pending one --
    re-attempting it every run would re-pay the throttle for an artist
    MusicBrainz has already answered "no" for."""
    conn = store.connect()
    graph_conn = graph_store.connect()
    _library(conn, ("a", "Some Obscure Act"))
    graph_store.put_brainz_artist(graph_conn, "some obscure act", mbid=None, name=None, status="no_match")

    assert maintain._pending_brainz_artists(conn, graph_conn, limit=None) == []


def test_pending_brainz_artists_respects_the_limit():
    conn = store.connect()
    graph_conn = graph_store.connect()
    _library(conn, ("a", "Arijit Singh"), ("b", "AP Dhillon"))

    assert maintain._pending_brainz_artists(conn, graph_conn, limit=1) == ["ap dhillon"]


def test_pending_brainz_artists_ignores_tracks_outside_the_library():
    """`track` rows persist for candidates that were ever gathered, not just
    library tracks -- only songs the user actually owns should pay the
    warm-up cost ahead of time."""
    conn = store.connect()
    graph_conn = graph_store.connect()
    store.upsert_tracks(conn, [{"videoId": "x", "title": "T", "artists": "Someone Else"}])
    _library(conn, ("a", "Arijit Singh"))

    assert maintain._pending_brainz_artists(conn, graph_conn, limit=None) == ["arijit singh"]
