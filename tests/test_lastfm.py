"""Unit tests for the last.fm track-level source (PLAN.md 7.13).

No network: last.fm is faked at `lastfm._get`. The error contract pinned here
was measured live: error 6 is a 200 and a real "not found", an empty `track`
list is a real "no neighbours", and a bad key is a 403 carrying error 10.
"""

import urllib.error

import graph
import graph_store
import lastfm

KEY = "test-key"
NOT_FOUND = {"error": 6, "message": "Track not found", "links": []}
RATE_LIMITED = {"error": 29, "message": "Rate limit exceeded"}


def _similar(*pairs):
    return {"similartracks": {"track": [
        {"name": title, "artist": {"name": artist}, "match": score} for title, artist, score in pairs
    ]}}


BROWN_MUNDE = _similar(("Born to Shine", "Diljit Dosanjh", 1.0), ("Wavy", "Karan Aujla", 0.8))


class _FakeLastfm:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if self.exc:
            raise self.exc
        return self.payload


def _wire(monkeypatch, fake, key=KEY):
    if key:
        monkeypatch.setenv("LASTFM_API_KEY", key)
    monkeypatch.setattr(lastfm, "_get", fake)
    return fake


def test_no_key_means_the_source_is_off_and_nothing_is_cached(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeLastfm(BROWN_MUNDE), key=None)
    assert lastfm.similar_tracks(graph_db, "Brown Munde", "AP Dhillon", sleep=lambda _s: None) == []
    assert fake.calls == []
    assert not graph_store.was_fetched(graph_db, lastfm._EP_SIMILAR, "brownmunde|ap dhillon")


def test_similar_tracks_are_parsed_in_order_and_cached(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeLastfm(BROWN_MUNDE))

    first = lastfm.similar_tracks(graph_db, "Brown Munde", "AP Dhillon", sleep=lambda _s: None)
    again = lastfm.similar_tracks(graph_db, "Brown Munde", "AP Dhillon", sleep=lambda _s: None)

    assert [r["title"] for r in first] == [r["title"] for r in again] == ["Born to Shine", "Wavy"]
    assert first[0]["artist_name"] == "Diljit Dosanjh"
    assert len(fake.calls) == 1
    assert "autocorrect=1" in fake.calls[0]


def test_limit_trims_the_answer_but_not_the_cache(graph_db, monkeypatch):
    """The cache holds the full fetch, so a later caller wanting more is not starved."""
    _wire(monkeypatch, _FakeLastfm(BROWN_MUNDE))
    assert len(lastfm.similar_tracks(graph_db, "Brown Munde", "AP Dhillon", limit=1, sleep=lambda _s: None)) == 1
    assert len(lastfm.similar_tracks(graph_db, "Brown Munde", "AP Dhillon", limit=10, sleep=lambda _s: None)) == 2


def test_track_not_found_is_a_cacheable_empty_answer(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeLastfm(NOT_FOUND))
    assert lastfm.similar_tracks(graph_db, "Nothing", "Nobody", sleep=lambda _s: None) == []
    assert lastfm.similar_tracks(graph_db, "Nothing", "Nobody", sleep=lambda _s: None) == []
    assert len(fake.calls) == 1


def test_an_empty_neighbour_list_is_a_cacheable_answer(graph_db, monkeypatch):
    """Kesariya, measured: known to last.fm, no neighbours under any spelling."""
    fake = _wire(monkeypatch, _FakeLastfm({"similartracks": {"track": []}}))
    lastfm.similar_tracks(graph_db, "Kesariya", "Arijit Singh", sleep=lambda _s: None)
    lastfm.similar_tracks(graph_db, "Kesariya", "Arijit Singh", sleep=lambda _s: None)
    assert len(fake.calls) == 1


def test_rate_limiting_is_not_cached(graph_db, monkeypatch):
    _wire(monkeypatch, _FakeLastfm(RATE_LIMITED))
    assert lastfm.similar_tracks(graph_db, "Brown Munde", "AP Dhillon", sleep=lambda _s: None) == []
    assert not graph_store.was_fetched(graph_db, lastfm._EP_SIMILAR, "brownmunde|ap dhillon")

    _wire(monkeypatch, _FakeLastfm(BROWN_MUNDE))
    assert len(lastfm.similar_tracks(graph_db, "Brown Munde", "AP Dhillon", sleep=lambda _s: None)) == 2


def test_a_rejected_key_is_not_cached(graph_db, monkeypatch):
    """A bad key is a 403, so it surfaces as an HTTPError, not an error body."""
    denied = urllib.error.HTTPError("https://ws.audioscrobbler.com", 403, "Forbidden", {}, None)
    _wire(monkeypatch, _FakeLastfm(exc=denied), key="bad")
    assert lastfm.similar_tracks(graph_db, "Brown Munde", "AP Dhillon", sleep=lambda _s: None) == []
    assert not graph_store.was_fetched(graph_db, lastfm._EP_SIMILAR, "brownmunde|ap dhillon")


def test_a_network_failure_is_not_cached(graph_db, monkeypatch):
    _wire(monkeypatch, _FakeLastfm(exc=OSError("network down")))
    assert lastfm.similar_tracks(graph_db, "Brown Munde", "AP Dhillon", sleep=lambda _s: None) == []
    assert not graph_store.was_fetched(graph_db, lastfm._EP_SIMILAR, "brownmunde|ap dhillon")


class _FakeDeezer:
    def __init__(self, routes):
        self.routes = routes

    def __call__(self, url):
        for fragment, payload in self.routes.items():
            if fragment in url:
                return payload
        return {"data": []}


def _dz_track(track_id, title, artist_name, artist_id):
    return {"id": track_id, "title": title, "artist": {"name": artist_name, "id": artist_id}}


def test_neighbours_tag_lastfm_candidates_distinctly(graph_db, monkeypatch):
    monkeypatch.setattr(graph, "_get", _FakeDeezer({
        "q=Born%20to%20Shine": {"data": [_dz_track(8001, "Born to Shine", "Diljit Dosanjh", 400)]},
        "q=Wavy": {"data": [_dz_track(8002, "Wavy", "Karan Aujla", 401)]},
    }))
    fake = _wire(monkeypatch, _FakeLastfm(BROWN_MUNDE))

    rows = graph.neighbours(
        graph_db,
        {"id": 1, "title": "Brown Munde", "artist_id": 100, "artist_name": "Deezer Credit"},
        include_radio=False,
        brainz_artist="AP Dhillon",
        sleep=lambda _s: None,
    )

    sources = {r["title"]: r["source"] for r in rows}
    assert sources.get("Born to Shine") == sources.get("Wavy") == "graph_similar_lfm"
    # The provider's credit, as for every second-source lookup.
    assert "AP+Dhillon" in fake.calls[0] and "Deezer" not in fake.calls[0]


class _FakeLastfmByArtist:
    """Routes on the `artist=` query param, the way last.fm's own listener
    data is split by credit -- see the composer-credit fallback below."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if f"artist={fragment}" in url:
                return payload
        return _similar()


def test_neighbours_fall_back_to_composer_credit_when_performer_credit_is_empty(graph_db, monkeypatch):
    """PLAN.md 7.13 residual: last.fm's listener data for a film song is often
    split across the performer credit (thin) and the composer credit (Deezer's
    own), so a miss on the first is retried under the second before giving up."""
    monkeypatch.setattr(graph, "_get", _FakeDeezer({
        "q=Kabira": {"data": [_dz_track(8003, "Kabira", "Pritam", 402)]},
    }))
    fake = _wire(monkeypatch, _FakeLastfmByArtist({"Pritam": _similar(("Kabira", "Pritam", 1.0))}))

    rows = graph.neighbours(
        graph_db,
        {"id": 1, "title": "Channa Mereya", "artist_id": 100, "artist_name": "Pritam"},
        include_radio=False,
        brainz_artist="Arijit Singh",
        sleep=lambda _s: None,
    )

    assert [r["title"] for r in rows] == ["Kabira"]
    assert rows[0]["source"] == "graph_similar_lfm"
    assert len(fake.calls) == 2
    assert "artist=Arijit+Singh" in fake.calls[0]
    assert "artist=Pritam" in fake.calls[1]


def test_neighbours_do_not_retry_lastfm_when_credits_are_the_same(graph_db, monkeypatch):
    """No second call when there is no distinct composer credit to fall back to."""
    monkeypatch.setattr(graph, "_get", _FakeDeezer({}))
    fake = _wire(monkeypatch, _FakeLastfmByArtist({}))

    rows = graph.neighbours(
        graph_db,
        {"id": 1, "title": "Excuses", "artist_id": 100, "artist_name": "AP Dhillon"},
        include_radio=False,
        sleep=lambda _s: None,
    )

    assert rows == []
    assert len(fake.calls) == 1


def test_neighbours_survive_lastfm_raising(graph_db, monkeypatch):
    """A supplement that breaks the primary path is not a supplement."""
    monkeypatch.setattr(graph, "_get", _FakeDeezer({
        "/artist/100/top": {"data": [_dz_track(9000, "Excuses", "AP Dhillon", 100)]},
    }))
    monkeypatch.setenv("LASTFM_API_KEY", KEY)
    monkeypatch.setattr(lastfm, "similar_tracks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    rows = graph.neighbours(
        graph_db, {"id": 1, "title": "X", "artist_id": 100, "artist_name": "AP Dhillon"},
        include_radio=False, sleep=lambda _s: None,
    )
    assert [r["title"] for r in rows] == ["Excuses"]
