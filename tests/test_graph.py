"""Unit tests for the neutral music graph and its cache.

No network: Deezer is faked at `graph._get`, the same transport boundary
test_filters.py fakes for tempo. Every test uses the `graph_db` fixture, which
is a temp file -- the real graph cache is shared by every provider instance and
must never be touched by a test run.
"""

import pytest

import graph
import graph_store


class _FakeDeezer:
    """Routes Deezer URLs to canned payloads, recording every call.

    Keyed by URL fragment rather than exact URL so tests state only the part
    they care about.
    """

    def __init__(self, routes=None, fail=False):
        self.routes = routes or {}
        self.fail = fail
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("network down")
        for fragment, payload in self.routes.items():
            if fragment in url:
                return payload
        return {"data": []}


def _wire(monkeypatch, fake):
    monkeypatch.setattr(graph, "_get", fake)
    return fake


def _track(track_id, title, artist_name, artist_id=99):
    return {"id": track_id, "title": title, "artist": {"name": artist_name, "id": artist_id}}


# --- search / resolve -------------------------------------------------------


def test_search_tracks_keeps_credit_matched_hits(monkeypatch):
    _wire(monkeypatch, _FakeDeezer({"/search": {"data": [_track(1, "Excuses", "AP Dhillon")]}}))
    rows = graph.search_tracks("Excuses", "AP Dhillon", sleep=lambda _s: None)
    assert [(r["id"], r["matched"]) for r in rows] == [(1, True)]
    assert rows[0]["artist_id"] == 99


def test_search_tracks_marks_an_unmatched_hit_rather_than_dropping_it(monkeypatch):
    # A rejected hit is still the best guess at which Deezer record this is --
    # tempo.py records it alongside a no-match status.
    _wire(monkeypatch, _FakeDeezer({"/search": {"data": [_track(7, "Something Else", "Someone")]}}))
    rows = graph.search_tracks("Excuses", "AP Dhillon", sleep=lambda _s: None)
    assert [(r["id"], r["matched"]) for r in rows] == [(7, False)]


def test_search_tracks_without_a_title_makes_no_request(monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer())
    assert graph.search_tracks("", None, sleep=lambda _s: None) == []
    assert fake.calls == []


def test_search_tracks_survives_a_network_failure(monkeypatch):
    _wire(monkeypatch, _FakeDeezer(fail=True))
    assert graph.search_tracks("Excuses", "AP Dhillon", sleep=lambda _s: None) == []


def test_resolve_caches_the_identity(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer({"/search": {"data": [_track(1, "Excuses", "AP Dhillon")]}}))
    first = graph.resolve(graph_db, "Excuses", "AP Dhillon", sleep=lambda _s: None)
    assert first["id"] == 1
    before = len(fake.calls)
    again = graph.resolve(graph_db, "Excuses", "AP Dhillon", sleep=lambda _s: None)
    assert again["id"] == 1
    assert len(fake.calls) == before, "a cached resolution must not re-hit the network"


def test_resolve_caches_a_negative_result_too(graph_db, monkeypatch):
    # tempo.py's lesson: rediscovering that Deezer has nothing costs two
    # searches every pass forever otherwise.
    fake = _wire(monkeypatch, _FakeDeezer({"/search": {"data": []}}))
    assert graph.resolve(graph_db, "Nonexistent", "Nobody", sleep=lambda _s: None) is None
    before = len(fake.calls)
    assert graph.resolve(graph_db, "Nonexistent", "Nobody", sleep=lambda _s: None) is None
    assert len(fake.calls) == before


def test_resolve_shares_one_row_across_upload_variants(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer({"/search": {"data": [_track(1, "Excuses", "AP Dhillon")]}}))
    graph.resolve(graph_db, "Excuses", "AP Dhillon", sleep=lambda _s: None)
    before = len(fake.calls)
    # Normalised keys mean the bracketed variant is the same cache row.
    assert graph.resolve(graph_db, "Excuses (Official Video)", "AP Dhillon", sleep=lambda _s: None)["id"] == 1
    assert len(fake.calls) == before


def test_resolve_ignores_an_unmatched_hit(graph_db, monkeypatch):
    # A credit-rejected hit is not identity, however useful it is to tempo.
    _wire(monkeypatch, _FakeDeezer({"/search": {"data": [_track(7, "Something Else", "Someone")]}}))
    assert graph.resolve(graph_db, "Excuses", "AP Dhillon", sleep=lambda _s: None) is None


def test_resolve_rejects_an_unusable_title(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer())
    assert graph.resolve(graph_db, "!!!", "AP Dhillon", sleep=lambda _s: None) is None
    assert fake.calls == []


def test_resolve_artist_matches_loosely(graph_db, monkeypatch):
    _wire(monkeypatch, _FakeDeezer({"/search/artist": {"data": [{"id": 5, "name": "AP Dhillon"}]}}))
    assert graph.resolve_artist(graph_db, "ap dhillon", sleep=lambda _s: None) == {"id": 5, "name": "AP Dhillon"}


def test_resolve_artist_without_a_name_makes_no_request(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer())
    assert graph.resolve_artist(graph_db, "", sleep=lambda _s: None) is None
    assert fake.calls == []


def test_search_ignores_a_whitespace_only_query(monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer())
    assert graph._search("   ", lambda _s: None) == []
    assert fake.calls == []


def test_artist_tracks_of_no_artist_makes_no_request(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer())
    assert graph.artist_tracks(graph_db, 0, graph.KIND_TOP, sleep=lambda _s: None) == []
    assert fake.calls == []


def test_track_detail_returns_none_on_a_network_failure(monkeypatch):
    _wire(monkeypatch, _FakeDeezer(fail=True))
    assert graph.track_detail(1, sleep=lambda _s: None) is None


def test_resolve_artist_returns_none_for_an_unrelated_hit(graph_db, monkeypatch):
    _wire(monkeypatch, _FakeDeezer({"/search/artist": {"data": [{"id": 5, "name": "Someone Else"}]}}))
    assert graph.resolve_artist(graph_db, "AP Dhillon", sleep=lambda _s: None) is None


# --- adjacency --------------------------------------------------------------


def test_related_artists_are_cached(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer(
        {"/related": {"data": [{"id": 2, "name": "Diljit Dosanjh"}, {"id": 3, "name": "Shubh"}]}}
    ))
    first = graph.related_artists(graph_db, 1, sleep=lambda _s: None)
    assert [a["name"] for a in first] == ["Diljit Dosanjh", "Shubh"]
    before = len(fake.calls)
    assert [a["name"] for a in graph.related_artists(graph_db, 1, sleep=lambda _s: None)] == [
        "Diljit Dosanjh", "Shubh",
    ]
    assert len(fake.calls) == before


def test_an_empty_related_result_is_cached_not_refetched(graph_db, monkeypatch):
    # The whole point of graph_fetch: "asked, nothing there" must not look
    # identical to "never asked", or it re-hits the network forever.
    fake = _wire(monkeypatch, _FakeDeezer({"/related": {"data": []}}))
    assert graph.related_artists(graph_db, 1, sleep=lambda _s: None) == []
    before = len(fake.calls)
    assert graph.related_artists(graph_db, 1, sleep=lambda _s: None) == []
    assert len(fake.calls) == before


def test_related_artists_of_no_artist_makes_no_request(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer())
    assert graph.related_artists(graph_db, 0, sleep=lambda _s: None) == []
    assert fake.calls == []


def test_artist_tracks_keeps_top_and_radio_separate(graph_db, monkeypatch):
    # Radio is best-effort and unevenly populated; pooling it with /top would
    # hide which evidence a candidate actually came from.
    _wire(monkeypatch, _FakeDeezer({
        "/top": {"data": [_track(10, "Top Song", "AP Dhillon")]},
        "/radio": {"data": [_track(20, "Radio Song", "AP Dhillon")]},
    }))
    top = graph.artist_tracks(graph_db, 1, graph.KIND_TOP, sleep=lambda _s: None)
    radio = graph.artist_tracks(graph_db, 1, graph.KIND_RADIO, sleep=lambda _s: None)
    assert [t["id"] for t in top] == [10]
    assert [t["id"] for t in radio] == [20]
    assert graph_store.get_artist_tracks(graph_db, 1, graph.KIND_TOP)[0]["title"] == "Top Song"


def test_artist_tracks_are_cached(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer({"/top": {"data": [_track(10, "Top Song", "AP Dhillon")]}}))
    graph.artist_tracks(graph_db, 1, graph.KIND_TOP, sleep=lambda _s: None)
    before = len(fake.calls)
    assert len(graph.artist_tracks(graph_db, 1, graph.KIND_TOP, sleep=lambda _s: None)) == 1
    assert len(fake.calls) == before


def test_artist_tracks_survive_a_network_failure(graph_db, monkeypatch):
    _wire(monkeypatch, _FakeDeezer(fail=True))
    assert graph.artist_tracks(graph_db, 1, graph.KIND_TOP, sleep=lambda _s: None) == []


# --- playlists --------------------------------------------------------------


def test_search_playlists_returns_readable_playlists(monkeypatch):
    # Deezer permits what Spotify forbids: finding playlists AND reading them.
    _wire(monkeypatch, _FakeDeezer(
        {"/search/playlist": {"data": [{"id": 42, "title": "Punjabi Sad", "nb_tracks": 100}]}}
    ))
    assert graph.search_playlists("punjabi sad", sleep=lambda _s: None) == [
        {"id": 42, "title": "Punjabi Sad", "track_count": 100}
    ]


def test_search_playlists_ignores_a_blank_query(monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer())
    assert graph.search_playlists("   ", sleep=lambda _s: None) == []
    assert fake.calls == []


def test_playlist_tracks_are_flattened(monkeypatch):
    _wire(monkeypatch, _FakeDeezer({"/tracks": {"data": [_track(1, "A", "X", artist_id=5)]}}))
    rows = graph.playlist_tracks(42, sleep=lambda _s: None)
    assert rows == [{"id": 1, "title": "A", "artist_name": "X", "artist_id": 5}]


# --- neighbours -------------------------------------------------------------


def test_neighbours_pool_artist_related_and_radio(graph_db, monkeypatch):
    _wire(monkeypatch, _FakeDeezer({
        "/artist/1/top": {"data": [_track(10, "Own Song", "AP Dhillon", artist_id=1)]},
        "/artist/1/radio": {"data": [_track(11, "Radio Song", "AP Dhillon", artist_id=1)]},
        "/artist/1/related": {"data": [{"id": 2, "name": "Shubh"}]},
        "/artist/2/top": {"data": [_track(20, "Neighbour Song", "Shubh", artist_id=2)]},
    }))
    seed = {"id": 999, "artist_id": 1}
    out = graph.neighbours(graph_db, seed, sleep=lambda _s: None)
    by_source = {c["source"] for c in out}
    assert by_source == {"graph_artist", "graph_radio", "graph_related"}
    assert {c["id"] for c in out} == {10, 11, 20}


def test_neighbours_never_returns_the_seed_itself(graph_db, monkeypatch):
    _wire(monkeypatch, _FakeDeezer({
        "/artist/1/top": {"data": [_track(10, "Seed", "AP Dhillon", artist_id=1)]},
    }))
    out = graph.neighbours(graph_db, {"id": 10, "artist_id": 1}, include_radio=False, sleep=lambda _s: None)
    assert out == []


def test_neighbours_without_a_resolved_artist_returns_nothing(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer())
    assert graph.neighbours(graph_db, {"id": 1, "artist_id": None}, sleep=lambda _s: None) == []
    assert fake.calls == []


def test_neighbours_can_skip_radio(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer({
        "/artist/1/top": {"data": [_track(10, "Own Song", "AP Dhillon", artist_id=1)]},
    }))
    graph.neighbours(graph_db, {"id": 999, "artist_id": 1}, include_radio=False, sleep=lambda _s: None)
    assert not any("/radio" in url for url in fake.calls)


# --- store bookkeeping ------------------------------------------------------


def test_was_fetched_distinguishes_never_asked_from_asked_and_empty(graph_db):
    assert not graph_store.was_fetched(graph_db, "artist_related", 1)
    graph_store.record_fetch(graph_db, "artist_related", 1, graph_store.STATUS_OK, 0)
    assert graph_store.was_fetched(graph_db, "artist_related", 1)


def test_graph_stats_report_what_is_cached(graph_db):
    graph_store.put_resolution(
        graph_db, "excuses", "ap dhillon",
        track_id=1, artist_id=2, title="Excuses", artist_name="AP Dhillon",
        status=graph_store.STATUS_OK,
    )
    graph_store.put_resolution(
        graph_db, "nothing", "nobody",
        track_id=None, artist_id=None, title=None, artist_name=None,
        status=graph_store.STATUS_NO_MATCH,
    )
    graph_store.put_related_artists(graph_db, 2, [{"id": 3, "name": "Shubh"}])
    stats = graph_store.stats(graph_db)
    assert stats["resolved_tracks"] == 1
    assert stats["unresolved_tracks"] == 1
    assert stats["artists_with_related"] == 1


def test_graph_db_is_not_scoped_per_provider(monkeypatch):
    # The deliberate inverse of store.py: Deezer ids are service-neutral, so
    # both provider instances must share one graph cache rather than each
    # resolving every artist independently.
    import provider

    monkeypatch.setenv("RECOM_PROVIDER", "spotify")
    assert provider.scoped_path(graph_store.DEFAULT_DB_PATH) != graph_store.DEFAULT_DB_PATH
    assert graph_store.DEFAULT_DB_PATH.name == "graph.db"
