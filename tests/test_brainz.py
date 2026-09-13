"""Unit tests for the second graph source (PLAN.md 7.3).

No network: MusicBrainz and ListenBrainz are both faked at `brainz._get_safe`,
the single transport boundary this module has -- the same approach
test_graph.py takes with Deezer.

The behaviours worth pinning here are the ones the measurement in `brainz.py`'s
header forced: MusicBrainz costs a throttled second per lookup so nothing may
be fetched twice, ListenBrainz coverage is genuinely partial so an empty answer
must be a cached fact rather than a retry forever, and the whole source is a
supplement so its failure must degrade to Deezer-only rather than propagate.
"""

import pytest

import brainz
import graph
import graph_store


class _FakeBrainz:
    """Routes MusicBrainz/ListenBrainz URLs to canned payloads, recording calls."""

    def __init__(self, routes=None, fail=False):
        self.routes = routes or {}
        self.fail = fail
        self.calls = []
        self.auths = []

    def __call__(self, url, tries=3, sleep=None, auth=None):
        self.calls.append(url)
        self.auths.append(auth)
        if self.fail:
            # The transport's own "could not ask" -- NOT a None, which at this
            # boundary means "asked, no answer" and is legitimately cacheable.
            return brainz.UNAVAILABLE
        for fragment, payload in self.routes.items():
            if fragment in url:
                return payload
        return None


def _artist_hit(name, mbid):
    return {"artists": [{"id": mbid, "name": name}]}


def _similar(*pairs):
    return [
        {"artist_mbid": mbid, "name": name, "score": score}
        for name, mbid, score in pairs
    ]


AP_MBID = "ed29f721-0d40-4a90-9f25-a91c2ccccd5e"

ROUTES = {
    "/artist?query": _artist_hit("AP Dhillon", AP_MBID),
    "similar-artists": _similar(
        ("Gurinder Gill", "56743d2c-b315-4d01-a444-fa42088182c0", 81),
        ("Shubh", "ef2686fe-0bdf-4e0c-b902-cc6609acc95d", 73),
    ),
}


def _wire(monkeypatch, fake):
    monkeypatch.setattr(brainz, "_get_safe", fake)
    return fake


# --- identity ---------------------------------------------------------------


def test_resolve_artist_caches_the_mbid(graph_db, monkeypatch):
    """MusicBrainz costs a throttled second per lookup, so once is the budget."""
    fake = _wire(monkeypatch, _FakeBrainz(ROUTES))

    first = brainz.resolve_artist(graph_db, "AP Dhillon", sleep=lambda _s: None)
    assert first["mbid"] == AP_MBID

    again = brainz.resolve_artist(graph_db, "AP Dhillon", sleep=lambda _s: None)
    assert again["mbid"] == AP_MBID
    assert len(fake.calls) == 1


def test_resolve_artist_caches_a_negative_result_too(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeBrainz({"/artist?query": {"artists": []}}))

    assert brainz.resolve_artist(graph_db, "Nobody At All", sleep=lambda _s: None) is None
    assert brainz.resolve_artist(graph_db, "Nobody At All", sleep=lambda _s: None) is None
    assert len(fake.calls) == 1


def test_resolve_artist_applies_the_same_credit_gate_as_deezer(graph_db, monkeypatch):
    """A hit whose name is a different artist is not identity, it is a near-miss."""
    _wire(monkeypatch, _FakeBrainz({"/artist?query": _artist_hit("Someone Else", AP_MBID)}))
    assert brainz.resolve_artist(graph_db, "AP Dhillon", sleep=lambda _s: None) is None


def test_a_503_is_not_cached_as_a_permanent_miss(graph_db, monkeypatch):
    """MusicBrainz 503s constantly. Caching that as "no such artist" would let
    one transient rate-limit poison an artist forever, indistinguishably from a
    real miss. Found live: a 503 had silently emptied Arijit Singh."""
    down = _FakeBrainz(fail=True)
    _wire(monkeypatch, down)
    assert brainz.resolve_artist(graph_db, "Arijit Singh", sleep=lambda _s: None) is None
    assert graph_store.get_brainz_artist(graph_db, "arijit singh") is None

    # The service comes back; the artist must resolve rather than stay poisoned.
    _wire(monkeypatch, _FakeBrainz({"/artist?query": _artist_hit("Arijit Singh", "abc-123")}))
    assert brainz.resolve_artist(graph_db, "Arijit Singh", sleep=lambda _s: None)["mbid"] == "abc-123"


def test_an_unreachable_listenbrainz_is_not_recorded_as_childless(graph_db, monkeypatch):
    """Unreachable and childless are both "no rows". Only one may be cached."""
    _wire(monkeypatch, _FakeBrainz(fail=True))
    assert brainz.related_artists(graph_db, AP_MBID, sleep=lambda _s: None) == []
    assert not graph_store.was_fetched(graph_db, brainz._EP_LB_RELATED, AP_MBID)

    _wire(monkeypatch, _FakeBrainz(ROUTES))
    rows = brainz.related_artists(graph_db, AP_MBID, sleep=lambda _s: None)
    assert [r["name"] for r in rows] == ["Gurinder Gill", "Shubh"]


def test_a_genuine_empty_answer_is_still_cached(graph_db, monkeypatch):
    """The 503 fix must not cost the Dua Lipa case its cached negative."""
    fake = _wire(monkeypatch, _FakeBrainz({"similar-artists": []}))
    brainz.related_artists(graph_db, AP_MBID, sleep=lambda _s: None)
    brainz.related_artists(graph_db, AP_MBID, sleep=lambda _s: None)
    assert len(fake.calls) == 1


# --- adjacency --------------------------------------------------------------


def test_related_artists_caches_including_the_empty_answer(graph_db, monkeypatch):
    """Dua Lipa really does return zero. That is an answer, not a miss."""
    fake = _wire(monkeypatch, _FakeBrainz({"similar-artists": []}))

    assert brainz.related_artists(graph_db, AP_MBID, sleep=lambda _s: None) == []
    assert brainz.related_artists(graph_db, AP_MBID, sleep=lambda _s: None) == []
    assert len(fake.calls) == 1


def test_related_artists_preserves_listenbrainz_order(graph_db, monkeypatch):
    _wire(monkeypatch, _FakeBrainz(ROUTES))
    rows = brainz.related_artists(graph_db, AP_MBID, sleep=lambda _s: None)
    assert [r["name"] for r in rows] == ["Gurinder Gill", "Shubh"]
    assert rows[0]["score"] == 81


def test_related_artist_names_is_the_whole_source_in_one_call(graph_db, monkeypatch):
    _wire(monkeypatch, _FakeBrainz(ROUTES))
    names = brainz.related_artist_names(graph_db, "AP Dhillon", sleep=lambda _s: None)
    assert names == ["Gurinder Gill", "Shubh"]


def test_related_artist_names_is_empty_when_identity_fails(graph_db, monkeypatch):
    """No MBID means no adjacency, and must not reach ListenBrainz at all."""
    fake = _wire(monkeypatch, _FakeBrainz({"/artist?query": {"artists": []}}))
    assert brainz.related_artist_names(graph_db, "Nobody At All", sleep=lambda _s: None) == []
    assert not any("similar-artists" in c for c in fake.calls)


# --- integration with the Deezer graph --------------------------------------


class _FakeDeezer:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                return payload
        return {"data": []}


def _dz_track(track_id, title, artist_name, artist_id):
    return {"id": track_id, "title": title, "artist": {"name": artist_name, "id": artist_id}}


def test_neighbours_tags_listenbrainz_candidates_distinctly(graph_db, monkeypatch):
    """The tag is the whole mechanism: two sources on one candidate is what
    signals._merge_and_score already reads as agreement."""
    monkeypatch.setattr(graph, "_get", _FakeDeezer({
        "/search/artist": {"data": [{"id": 501, "name": "Gurinder Gill"}]},
        "/artist/501/top": {"data": [_dz_track(9001, "Dream", "Gurinder Gill", 501)]},
        "/artist/100/top": {"data": [_dz_track(9000, "Excuses", "AP Dhillon", 100)]},
    }))
    _wire(monkeypatch, _FakeBrainz(ROUTES))

    rows = graph.neighbours(
        graph_db,
        {"id": 1, "artist_id": 100, "artist_name": "AP Dhillon"},
        include_radio=False,
        sleep=lambda _s: None,
    )
    sources = {r["title"]: r["source"] for r in rows}
    assert sources["Dream"] == "graph_related_lb"


def test_neighbours_degrades_to_deezer_when_the_second_source_fails(graph_db, monkeypatch):
    """A supplement that breaks the primary path is not a supplement."""
    monkeypatch.setattr(graph, "_get", _FakeDeezer({
        "/artist/100/top": {"data": [_dz_track(9000, "Excuses", "AP Dhillon", 100)]},
    }))
    _wire(monkeypatch, _FakeBrainz(fail=True))

    rows = graph.neighbours(
        graph_db,
        {"id": 1, "artist_id": 100, "artist_name": "AP Dhillon"},
        include_radio=False,
        sleep=lambda _s: None,
    )
    assert [r["title"] for r in rows] == ["Excuses"]


def test_neighbours_does_not_expand_an_artist_both_sources_agree_on(graph_db, monkeypatch):
    """Overlap is real (Jaccard 0.137, not 0). Crawling it twice is waste."""
    deezer = _FakeDeezer({
        "/search/artist": {"data": [{"id": 501, "name": "Gurinder Gill"}]},
        "/artist/100/related": {"data": [{"id": 501, "name": "Gurinder Gill"}]},
        "/artist/501/top": {"data": [_dz_track(9001, "Dream", "Gurinder Gill", 501)]},
        "/artist/100/top": {"data": [_dz_track(9000, "Excuses", "AP Dhillon", 100)]},
    })
    monkeypatch.setattr(graph, "_get", deezer)
    _wire(monkeypatch, _FakeBrainz(ROUTES))

    rows = graph.neighbours(
        graph_db,
        {"id": 1, "artist_id": 100, "artist_name": "AP Dhillon"},
        include_radio=False,
        sleep=lambda _s: None,
    )
    # Deezer got there first, so the track keeps the Deezer tag and is not
    # duplicated under the ListenBrainz one.
    assert [r["source"] for r in rows if r["title"] == "Dream"] == ["graph_related"]


def test_neighbours_prefers_the_providers_credit_over_deezers(graph_db, monkeypatch):
    """Deezer credits "Kesariya" to Pritam, its composer; the provider credits
    Arijit Singh, who sings it. The performer is the right adjacency seed, and
    Indian film music makes this the common case rather than an edge one."""
    monkeypatch.setattr(graph, "_get", _FakeDeezer({
        "/search/artist": {"data": [{"id": 501, "name": "Gurinder Gill"}]},
        "/artist/501/top": {"data": [_dz_track(9001, "Dream", "Gurinder Gill", 501)]},
    }))
    asked = []

    class _Recording(_FakeBrainz):
        def __call__(self, url, tries=3, sleep=None):
            if "/artist?query" in url:
                asked.append(url)
            return super().__call__(url, tries, sleep)

    _wire(monkeypatch, _Recording(ROUTES))
    graph.neighbours(
        graph_db,
        {"id": 1, "artist_id": 100, "artist_name": "Pritam"},
        include_radio=False,
        brainz_artist="Arijit Singh",
        sleep=lambda _s: None,
    )
    assert any("Arijit" in u for u in asked)
    assert not any("Pritam" in u for u in asked)


# --- the cache the second source made mandatory -----------------------------


def test_graph_resolve_artist_now_caches(graph_db, monkeypatch):
    """Every LB neighbour crosses back into Deezer by name; uncached that is
    one search per neighbour per seed, forever."""
    deezer = _FakeDeezer({"/search/artist": {"data": [{"id": 501, "name": "Shubh"}]}})
    monkeypatch.setattr(graph, "_get", deezer)

    assert graph.resolve_artist(graph_db, "Shubh", sleep=lambda _s: None)["id"] == 501
    assert graph.resolve_artist(graph_db, "Shubh", sleep=lambda _s: None)["id"] == 501
    assert len(deezer.calls) == 1


def test_graph_resolve_artist_caches_the_negative(graph_db, monkeypatch):
    deezer = _FakeDeezer({"/search/artist": {"data": []}})
    monkeypatch.setattr(graph, "_get", deezer)

    assert graph.resolve_artist(graph_db, "Nobody", sleep=lambda _s: None) is None
    assert graph.resolve_artist(graph_db, "Nobody", sleep=lambda _s: None) is None
    assert len(deezer.calls) == 1


# --- track-level similarity (PLAN.md 7.12) ----------------------------------

TOKEN = "test-token"
CHANNA_MBID = "e27ee5d7-703d-4985-a183-e5eef0020fb5"
BULLEYA_MBID = "d238bda0-da14-4977-9e3d-a2fb276c31b8"

TRACK_ROUTES = {
    "metadata/lookup": {
        "recording_mbid": CHANNA_MBID,
        "recording_name": "Channa Mereya",
        "artist_credit_name": "Arijit Singh",
    },
    "similar-recordings": [
        {
            "recording_mbid": BULLEYA_MBID,
            "recording_name": "Bulleya",
            "artist_credit_name": "Amit Mishra & Shilpa Rao",
            "score": 23,
        }
    ],
}


def test_no_token_means_the_track_source_is_off_not_empty(graph_db, monkeypatch):
    """Without a token the lookup 401s. That is "not configured", and caching
    it as a miss would keep the source dark after a token is added."""
    fake = _wire(monkeypatch, _FakeBrainz(TRACK_ROUTES))

    assert brainz.resolve_recording(graph_db, "Channa Mereya", "Arijit Singh", sleep=lambda _s: None) is None
    assert fake.calls == []
    assert graph_store.get_brainz_recording(graph_db, "channamereya", "arijit singh") is None


def test_resolve_recording_sends_the_token_and_caches(graph_db, monkeypatch):
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", TOKEN)
    fake = _wire(monkeypatch, _FakeBrainz(TRACK_ROUTES))

    first = brainz.resolve_recording(graph_db, "Channa Mereya", "Arijit Singh", sleep=lambda _s: None)
    again = brainz.resolve_recording(graph_db, "Channa Mereya", "Arijit Singh", sleep=lambda _s: None)

    assert first["mbid"] == again["mbid"] == CHANNA_MBID
    assert len(fake.calls) == 1
    assert fake.auths == [TOKEN]


def test_resolve_recording_caches_the_empty_object_as_a_miss(graph_db, monkeypatch):
    """A lookup miss is `{}` with a 200, measured -- not a 404."""
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", TOKEN)
    fake = _wire(monkeypatch, _FakeBrainz({"metadata/lookup": {}}))

    assert brainz.resolve_recording(graph_db, "No Such Song", "Nobody", sleep=lambda _s: None) is None
    assert brainz.resolve_recording(graph_db, "No Such Song", "Nobody", sleep=lambda _s: None) is None
    assert len(fake.calls) == 1


def test_an_unauthorised_lookup_is_not_cached(graph_db, monkeypatch):
    """A revoked or mistyped token surfaces as UNAVAILABLE; fixing it must work."""
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", "bad")
    _wire(monkeypatch, _FakeBrainz(fail=True))
    assert brainz.resolve_recording(graph_db, "Channa Mereya", "Arijit Singh", sleep=lambda _s: None) is None

    _wire(monkeypatch, _FakeBrainz(TRACK_ROUTES))
    assert brainz.resolve_recording(graph_db, "Channa Mereya", "Arijit Singh", sleep=lambda _s: None)["mbid"] == CHANNA_MBID


def test_similar_recordings_caches_including_empty(graph_db, monkeypatch):
    fake = _wire(monkeypatch, _FakeBrainz({"similar-recordings": []}))

    assert brainz.similar_recordings(graph_db, "some-mbid", sleep=lambda _s: None) == []
    assert brainz.similar_recordings(graph_db, "some-mbid", sleep=lambda _s: None) == []
    assert len(fake.calls) == 1


def test_similar_recordings_uses_the_recording_algorithm_not_the_artist_one(graph_db, monkeypatch):
    """The artist algorithm name is a 400 here -- the bug that twice made this
    endpoint look empty."""
    fake = _wire(monkeypatch, _FakeBrainz(TRACK_ROUTES))

    rows = brainz.similar_recordings(graph_db, CHANNA_MBID, sleep=lambda _s: None)

    assert [r["title"] for r in rows] == ["Bulleya"]
    assert f"algorithm={brainz.LB_RECORDING_ALGORITHM}" in fake.calls[0]
    assert brainz.LB_ALGORITHM not in fake.calls[0]


def test_neighbours_tags_track_level_candidates_distinctly(graph_db, monkeypatch):
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", TOKEN)
    monkeypatch.setattr(graph, "_get", _FakeDeezer({
        "q=Bulleya": {"data": [_dz_track(7001, "Bulleya", "Amit Mishra", 300)]},
        "/artist/100/top": {"data": [_dz_track(1, "Channa Mereya", "Pritam", 100)]},
    }))
    fake = _wire(monkeypatch, _FakeBrainz(TRACK_ROUTES))

    rows = graph.neighbours(
        graph_db,
        {"id": 1, "title": "Channa Mereya", "artist_id": 100, "artist_name": "Pritam"},
        include_radio=False,
        brainz_artist="Arijit Singh",
        sleep=lambda _s: None,
    )

    assert {r["title"]: r["source"] for r in rows}.get("Bulleya") == "graph_similar_lb"
    # The provider's credit, not Deezer's composer credit, is what is looked up.
    lookup = next(u for u in fake.calls if "metadata/lookup" in u)
    assert "Arijit" in lookup and "Pritam" not in lookup


def test_neighbours_never_returns_the_seed_as_its_own_similar_track(graph_db, monkeypatch):
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", TOKEN)
    monkeypatch.setattr(graph, "_get", _FakeDeezer({
        "q=Bulleya": {"data": [_dz_track(1, "Bulleya", "Amit Mishra", 300)]},
    }))
    _wire(monkeypatch, _FakeBrainz(TRACK_ROUTES))

    rows = graph.neighbours(
        graph_db,
        {"id": 1, "title": "Channa Mereya", "artist_id": 100, "artist_name": "Arijit Singh"},
        include_radio=False,
        sleep=lambda _s: None,
    )
    assert all(r["id"] != 1 for r in rows)
