"""Unit tests for v6: capability gating, graph signals, and lazy resolution.

No network -- `graph._get` is faked, and conftest's `no_network` guard fails
the test if anything slips past that.

The through-line here is the thing v6 exists to fix: on a backend with no
native discovery signals, `recommend_from_song` used to return zero songs.
"""

import graph
import match
import provider
import server
import signals
import store


# --- fakes ------------------------------------------------------------------


class _FakeProvider:
    """A provider with declarable capabilities and a scripted search."""

    def __init__(self, capabilities=None, watch=None, artists=None, search_results=None, track_meta=None):
        self._capabilities = capabilities
        self._watch = watch or {}
        self._artists = artists or {}
        self._search_results = search_results or {}
        self._track_meta = track_meta or {}
        self.watch_calls = []
        self.artist_calls = []
        self.search_calls = []

    def capabilities(self):
        if self._capabilities is None:
            return set(provider.ALL_CAPABILITIES)
        return set(self._capabilities)

    def get_watch_playlist(self, videoId, limit=25, radio=True):
        self.watch_calls.append(videoId)
        return self._watch.get(videoId, {})

    def get_song_related(self, browseId):
        return []

    def get_artist(self, channelId):
        self.artist_calls.append(channelId)
        return self._artists.get(channelId, {})

    def get_track_meta(self, video_id):
        return self._track_meta.get(video_id)

    def search(self, query, filter=None, limit=20):
        self.search_calls.append(query)
        return self._search_results.get(query, [])

    def get_library_playlists(self, limit=25):
        return []

    def get_playlist(self, playlistId, limit=None):
        return {"tracks": []}

    def get_history(self):
        return []


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


def _dz_track(track_id, title, artist_name, artist_id=1):
    return {"id": track_id, "title": title, "artist": {"name": artist_name, "id": artist_id}}


def _deezer_for_ap_dhillon():
    """Seed resolves to artist 1; artist 1 has a catalogue and one neighbour."""
    return _FakeDeezer({
        "/search/playlist": {"data": []},
        "/search?": {"data": [_dz_track(100, "Excuses", "AP Dhillon", 1)]},
        "/artist/1/top": {"data": [_dz_track(101, "Brown Munde", "AP Dhillon", 1)]},
        "/artist/1/radio": {"data": [_dz_track(102, "Toxic", "AP Dhillon", 1)]},
        "/artist/1/related": {"data": [{"id": 2, "name": "Shubh"}]},
        "/artist/2/top": {"data": [_dz_track(200, "Elevated", "Shubh", 2)]},
    })


# --- capability gating ------------------------------------------------------


def test_capabilities_default_to_everything_for_a_backend_that_declares_none():
    # Every provider behaved this way before v6; an unmodified or third-party
    # Provider must keep working unchanged.
    class _Bare:
        pass

    assert provider.capabilities_of(_Bare()) == provider.ALL_CAPABILITIES


def test_declared_capabilities_are_honoured():
    fake = _FakeProvider(capabilities={provider.CAP_RADIO})
    assert provider.capabilities_of(fake) == {provider.CAP_RADIO}


def test_a_backend_without_radio_is_never_asked_for_it():
    # The wasted-call case: Spotify's get_watch_playlist also pays for a
    # restricted /recommendations call, so it must not be attempted at all.
    fake = _FakeProvider(capabilities=set())
    signals._gather_seed_candidates(fake, "seed1")
    assert fake.watch_calls == []


def test_a_backend_without_artist_capability_is_never_asked_for_artists():
    fake = _FakeProvider(
        capabilities={provider.CAP_RADIO},
        watch={"seed1": {"tracks": [
            {"videoId": "seed1", "title": "Seed", "artists": [{"name": "A", "id": "art1"}]},
            {"videoId": "v2", "title": "Other", "artists": [{"name": "B"}]},
        ]}},
    )
    found = signals._gather_seed_candidates(fake, "seed1")
    assert fake.artist_calls == []
    assert "v2" in found


def test_native_signals_still_run_when_declared():
    fake = _FakeProvider(
        watch={"seed1": {"tracks": [
            {"videoId": "seed1", "title": "Seed", "artists": [{"name": "A", "id": "art1"}]},
            {"videoId": "v2", "title": "Radio Song", "artists": [{"name": "B"}]},
        ]}},
        artists={"art1": {"songs": {"results": [
            {"videoId": "v3", "title": "Catalogue Song", "artists": [{"name": "A"}]}
        ]}}},
    )
    found = signals._gather_seed_candidates(fake, "seed1")
    assert found["v2"]["sources"] == {"radio"}
    assert found["v3"]["sources"] == {"artist"}


# --- graph signals ----------------------------------------------------------


def test_graph_supplies_candidates_when_the_backend_has_no_native_signals(graph_db, monkeypatch):
    # The headline case: a backend where every native signal is gone.
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    fake = _FakeProvider(capabilities=set())

    found = signals._gather_seed_candidates(
        fake, "seed1", graph_conn=graph_db,
        seed_meta={"title": "Excuses", "artists": ["AP Dhillon"]},
    )

    assert found, "a backend with no native signals must still get candidates"
    assert all(key.startswith(signals.GRAPH_KEY_PREFIX) for key in found)
    sources = {s for c in found.values() for s in c["sources"]}
    assert sources == {"graph_artist", "graph_radio", "graph_related"}


def test_graph_candidates_carry_no_provider_id_until_resolved(graph_db, monkeypatch):
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    found = signals._gather_seed_candidates(
        _FakeProvider(capabilities=set()), "seed1", graph_conn=graph_db,
        seed_meta={"title": "Excuses", "artists": ["AP Dhillon"]},
    )
    candidate = next(iter(found.values()))
    assert candidate["videoId"] is None
    assert candidate["graphRef"]["trackId"]


def test_graph_and_native_signals_pool_into_one_score(graph_db, monkeypatch):
    # Agreement across independent sources is the ranking rule, and the graph
    # is just another independent source.
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    fake = _FakeProvider(
        watch={"seed1": {"tracks": [
            {"videoId": "seed1", "title": "Excuses", "artists": [{"name": "AP Dhillon", "id": "art1"}]},
            {"videoId": "v2", "title": "Radio Song", "artists": [{"name": "B"}]},
        ]}},
    )
    found = signals._gather_seed_candidates(fake, "seed1", graph_conn=graph_db)
    assert "v2" in found
    assert any(k.startswith(signals.GRAPH_KEY_PREFIX) for k in found)


def test_graph_is_skipped_when_the_seed_cannot_be_resolved(graph_db, monkeypatch):
    monkeypatch.setattr(graph, "_get", _FakeDeezer({"/search": {"data": []}}))
    found = signals._gather_seed_candidates(
        _FakeProvider(capabilities=set()), "seed1", graph_conn=graph_db,
        seed_meta={"title": "Unknown Song", "artists": ["Nobody"]},
    )
    assert found == {}


def test_graph_is_off_when_no_connection_is_passed(monkeypatch):
    # v1 behaviour, exactly: no graph connection means no graph calls.
    fake_deezer = _FakeDeezer({})
    monkeypatch.setattr(graph, "_get", fake_deezer)
    signals._gather_seed_candidates(_FakeProvider(capabilities=set()), "seed1")
    assert fake_deezer.calls == []


# --- seed metadata ----------------------------------------------------------


def test_seed_metadata_prefers_a_radio_response_already_in_hand():
    fake = _FakeProvider()
    watch = {"tracks": [{"videoId": "seed1", "title": "Excuses", "artists": [{"name": "AP Dhillon"}]}]}
    meta = signals.seed_metadata(fake, "seed1", watch)
    assert meta["title"] == "Excuses"
    assert fake.watch_calls == []


def test_seed_metadata_falls_back_to_get_track_meta():
    fake = _FakeProvider(track_meta={"seed1": {"videoId": "seed1", "title": "Excuses", "artists": [{"name": "AP Dhillon"}]}})
    meta = signals.seed_metadata(fake, "seed1")
    assert meta["title"] == "Excuses"
    assert fake.watch_calls == [], "get_track_meta exists to avoid the watch call"


def test_seed_metadata_falls_back_to_the_watch_playlist():
    class _NoTrackMeta(_FakeProvider):
        get_track_meta = None

    fake = _NoTrackMeta(watch={"seed1": {"tracks": [
        {"videoId": "seed1", "title": "Excuses", "artists": [{"name": "AP Dhillon"}]}
    ]}})
    assert signals.seed_metadata(fake, "seed1")["title"] == "Excuses"
    assert fake.watch_calls == ["seed1"]


def test_seed_metadata_returns_none_when_nothing_knows_the_seed():
    assert signals.seed_metadata(_FakeProvider(), "unknown") is None


def test_seed_metadata_falls_through_a_failing_get_track_meta():
    # A failing optional method must not sink the request -- the universal
    # fallback still runs.
    class _BrokenMeta(_FakeProvider):
        def get_track_meta(self, video_id):
            raise server.ProviderError("track lookup down")

    fake = _BrokenMeta(watch={"seed1": {"tracks": [
        {"videoId": "seed1", "title": "Excuses", "artists": [{"name": "AP Dhillon"}]}
    ]}})
    assert signals.seed_metadata(fake, "seed1")["title"] == "Excuses"


def test_seed_metadata_gives_up_quietly_when_the_watch_call_fails():
    class _BrokenWatch(_FakeProvider):
        get_track_meta = None

        def get_watch_playlist(self, videoId, limit=25, radio=True):
            raise server.ProviderError("watch is down")

    assert signals.seed_metadata(_BrokenWatch(), "seed1") is None


def test_graph_is_skipped_when_the_seed_has_no_title(graph_db, monkeypatch):
    fake_deezer = _FakeDeezer({})
    monkeypatch.setattr(graph, "_get", fake_deezer)
    found = signals._gather_seed_candidates(
        _FakeProvider(capabilities=set()), "seed1", graph_conn=graph_db,
        seed_meta={"title": None, "artists": []},
    )
    assert found == {}
    assert fake_deezer.calls == []


# --- two-stage exclusion ----------------------------------------------------


def test_graph_candidates_are_excluded_by_title_before_they_have_an_id():
    # Stage one. Without it the never-recommend-a-library-song guarantee stops
    # applying to every graph candidate, which is the worst bug this project
    # could ship.
    merged = {
        "graph:101": {
            "videoId": None, "graphRef": {"trackId": 101, "artistId": 1},
            "title": "Brown Munde", "artists": ["AP Dhillon"], "album": None,
            "sources": {"graph_artist"}, "score": 1,
        },
    }
    index = match.build_index([("Brown Munde", "AP Dhillon")])
    songs, _ = signals._finalize(merged, set(), 10, exclude_index=index)
    assert songs == []


def test_graph_candidates_survive_when_not_in_the_library():
    merged = {
        "graph:101": {
            "videoId": None, "graphRef": {"trackId": 101, "artistId": 1},
            "title": "Brown Munde", "artists": ["AP Dhillon"], "album": None,
            "sources": {"graph_artist"}, "score": 1,
        },
    }
    index = match.build_index([("Something Else", "Someone")])
    songs, _ = signals._finalize(merged, set(), 10, exclude_index=index)
    assert [s["title"] for s in songs] == ["Brown Munde"]


def test_native_candidates_are_still_excluded_by_id():
    merged = {
        "v1": {"videoId": "v1", "title": "A", "artists": ["X"], "album": None,
               "sources": {"radio"}, "score": 1},
    }
    songs, _ = signals._finalize(merged, {"v1"}, 10)
    assert songs == []


def test_finalize_without_an_index_keeps_graph_candidates():
    # An empty/absent index must not silently drop everything -- stage two is
    # what makes this safe.
    merged = {
        "graph:101": {
            "videoId": None, "graphRef": {"trackId": 101, "artistId": 1},
            "title": "Brown Munde", "artists": ["AP Dhillon"], "album": None,
            "sources": {"graph_artist"}, "score": 1,
        },
    }
    songs, _ = signals._finalize(merged, set(), 10, exclude_index=None)
    assert len(songs) == 1


# --- lazy resolution --------------------------------------------------------


def _graph_song(title, artist, track_id=101):
    return {
        "videoId": None, "graphRef": {"trackId": track_id, "artistId": 1},
        "title": title, "artists": [artist], "album": None, "score": 1,
        "sources": ["graph_artist"],
    }


def test_resolution_attaches_a_provider_id():
    fake = _FakeProvider(search_results={
        "Brown Munde AP Dhillon": [
            {"videoId": "yt1", "title": "Brown Munde", "artists": [{"name": "AP Dhillon"}]}
        ]
    })
    songs, dropped = signals.resolve_candidates(fake, [_graph_song("Brown Munde", "AP Dhillon")], 10)
    assert [s["videoId"] for s in songs] == ["yt1"]
    assert dropped == 0


def test_resolution_drops_a_near_miss_rather_than_substituting_it():
    fake = _FakeProvider(search_results={
        "Brown Munde AP Dhillon": [
            {"videoId": "yt9", "title": "A Totally Different Song", "artists": [{"name": "Someone"}]}
        ]
    })
    songs, dropped = signals.resolve_candidates(fake, [_graph_song("Brown Munde", "AP Dhillon")], 10)
    assert songs == []
    assert dropped == 1


def test_resolution_is_stage_two_of_exclusion():
    # The candidate cleared the text index but turns out to be a library song
    # once it has a real id. This is the authoritative check.
    fake = _FakeProvider(search_results={
        "Brown Munde AP Dhillon": [
            {"videoId": "yt1", "title": "Brown Munde", "artists": [{"name": "AP Dhillon"}]}
        ]
    })
    songs, dropped = signals.resolve_candidates(
        fake, [_graph_song("Brown Munde", "AP Dhillon")], 10, exclude={"yt1"}
    )
    assert songs == []
    assert dropped == 1


def test_resolution_skips_a_candidate_with_no_title():
    fake = _FakeProvider()
    songs, dropped = signals.resolve_candidates(
        fake, [{"videoId": None, "title": None, "artists": [], "album": None,
                "score": 1, "sources": ["graph_artist"]}], 10
    )
    assert (songs, dropped) == ([], 1)
    assert fake.search_calls == []


def test_resolution_ignores_a_search_hit_with_no_id():
    fake = _FakeProvider(search_results={
        "Brown Munde AP Dhillon": [
            {"title": "Brown Munde", "artists": [{"name": "AP Dhillon"}]},  # no videoId
            {"videoId": "yt1", "title": "Brown Munde", "artists": [{"name": "AP Dhillon"}]},
        ]
    })
    songs, _ = signals.resolve_candidates(fake, [_graph_song("Brown Munde", "AP Dhillon")], 10)
    assert [s["videoId"] for s in songs] == ["yt1"]


def test_resolution_searches_by_title_alone_when_no_artist_is_known():
    fake = _FakeProvider(search_results={
        "Brown Munde": [{"videoId": "yt1", "title": "Brown Munde", "artists": []}]
    })
    song = _graph_song("Brown Munde", "X")
    song["artists"] = []
    songs, _ = signals.resolve_candidates(fake, [song], 10)
    assert fake.search_calls == ["Brown Munde"]
    assert [s["videoId"] for s in songs] == ["yt1"]


def test_resolution_leaves_native_candidates_untouched():
    fake = _FakeProvider()
    native = {"videoId": "yt1", "title": "A", "artists": ["X"], "album": None,
              "score": 2, "sources": ["radio"]}
    songs, dropped = signals.resolve_candidates(fake, [native], 10)
    assert songs == [native]
    assert fake.search_calls == [], "a candidate with an id needs no search"


def test_resolution_strips_the_graph_reference_from_output():
    # graphRef is internal plumbing; it must not leak into a tool response.
    fake = _FakeProvider(search_results={
        "Brown Munde AP Dhillon": [
            {"videoId": "yt1", "title": "Brown Munde", "artists": [{"name": "AP Dhillon"}]}
        ]
    })
    songs, _ = signals.resolve_candidates(fake, [_graph_song("Brown Munde", "AP Dhillon")], 10)
    assert "graphRef" not in songs[0]


def test_resolution_stops_at_the_limit():
    fake = _FakeProvider(search_results={
        f"Song{i} X": [{"videoId": f"yt{i}", "title": f"Song{i}", "artists": [{"name": "X"}]}]
        for i in range(5)
    })
    pool = [_graph_song(f"Song{i}", "X", track_id=i) for i in range(5)]
    songs, _ = signals.resolve_candidates(fake, pool, 2)
    assert len(songs) == 2


def test_resolution_survives_a_provider_search_failure():
    class _Boom(_FakeProvider):
        def search(self, query, filter=None, limit=20):
            raise server.ProviderError("search is down")

    songs, dropped = signals.resolve_candidates(_Boom(), [_graph_song("Brown Munde", "AP Dhillon")], 10)
    assert songs == []
    assert dropped == 1


def test_resolution_does_no_work_when_native_candidates_fill_the_response():
    """The measured regression that forced lazy walking.

    On YouTube every native signal is available, so the top of the pool is
    already resolved and the graph candidates below it are dead weight.
    Resolving the whole pool eagerly took a 5.0s live request to 18.5s for an
    identical top ten.
    """
    fake = _FakeProvider()
    native = [
        {"videoId": f"yt{i}", "title": f"Native{i}", "artists": ["X"], "album": None,
         "score": 2, "sources": ["radio"]}
        for i in range(3)
    ]
    graph_tail = [_graph_song(f"Graph{i}", "Y", track_id=i) for i in range(10)]

    songs, _ = signals.resolve_candidates(fake, native + graph_tail, 3)
    assert len(songs) == 3
    assert fake.search_calls == [], "no graph candidate should have been resolved"


def test_resolution_reaches_further_down_only_as_needed():
    # One native candidate short of the limit: exactly one graph candidate
    # should be resolved, not the whole tail.
    fake = _FakeProvider(search_results={
        "Graph0 Y": [{"videoId": "g0", "title": "Graph0", "artists": [{"name": "Y"}]}],
        "Graph1 Y": [{"videoId": "g1", "title": "Graph1", "artists": [{"name": "Y"}]}],
        "Graph2 Y": [{"videoId": "g2", "title": "Graph2", "artists": [{"name": "Y"}]}],
    })
    native = [{"videoId": "yt0", "title": "Native0", "artists": ["X"], "album": None,
               "score": 2, "sources": ["radio"]}]
    tail = [_graph_song(f"Graph{i}", "Y", track_id=i) for i in range(3)]

    songs, _ = signals.resolve_candidates(fake, native + tail, 2)
    assert [s["videoId"] for s in songs] == ["yt0", "g0"]
    assert fake.search_calls == ["Graph0 Y"]


def test_resolution_keeps_going_past_a_failure_to_fill_the_limit():
    # A dropped candidate must not end the walk -- the next one down takes its
    # place, which is what the buffer pool exists to make possible.
    fake = _FakeProvider(search_results={
        "Graph1 Y": [{"videoId": "g1", "title": "Graph1", "artists": [{"name": "Y"}]}],
    })
    tail = [_graph_song(f"Graph{i}", "Y", track_id=i) for i in range(3)]
    songs, dropped = signals.resolve_candidates(fake, tail, 1)
    assert [s["videoId"] for s in songs] == ["g1"]
    assert dropped == 1


def test_resolution_respects_a_search_budget():
    """The language/tempo path asks for a pool 12x the limit.

    Native candidates are free to over-fetch; graph candidates cost a provider
    search each, so the deep pool must not turn into hundreds of round trips.
    """
    fake = _FakeProvider(search_results={
        f"Graph{i} Y": [{"videoId": f"g{i}", "title": f"Graph{i}", "artists": [{"name": "Y"}]}]
        for i in range(20)
    })
    tail = [_graph_song(f"Graph{i}", "Y", track_id=i) for i in range(20)]

    songs, _ = signals.resolve_candidates(fake, tail, 20, max_resolve=5)
    assert len(fake.search_calls) == 5
    assert len(songs) == 5


def test_an_unbounded_budget_still_resolves_everything():
    fake = _FakeProvider(search_results={
        f"Graph{i} Y": [{"videoId": f"g{i}", "title": f"Graph{i}", "artists": [{"name": "Y"}]}]
        for i in range(4)
    })
    tail = [_graph_song(f"Graph{i}", "Y", track_id=i) for i in range(4)]
    songs, _ = signals.resolve_candidates(fake, tail, 10)
    assert len(songs) == 4


def test_resolve_pool_size_leaves_room_for_losses():
    # Stage-two exclusion and unresolvable candidates both shrink the pool, so
    # resolving exactly `limit` would routinely return short.
    assert signals.resolve_pool_size(20) > 20
    assert signals.resolve_pool_size(1) > 1


# --- server wiring ----------------------------------------------------------


def test_recommend_from_song_returns_graph_results_with_no_native_signals(
    graph_enabled, monkeypatch
):
    """The measured failure v6 exists to fix, end to end.

    A backend with every native discovery signal revoked returned zero songs.
    It must now return graph-sourced ones.
    """
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    fake = _FakeProvider(
        capabilities=set(),
        track_meta={"seed1": {"videoId": "seed1", "title": "Excuses",
                              "artists": [{"name": "AP Dhillon"}]}},
        search_results={
            "Brown Munde AP Dhillon": [
                {"videoId": "sp1", "title": "Brown Munde", "artists": [{"name": "AP Dhillon"}]}],
            "Toxic AP Dhillon": [
                {"videoId": "sp2", "title": "Toxic", "artists": [{"name": "AP Dhillon"}]}],
            "Elevated Shubh": [
                {"videoId": "sp3", "title": "Elevated", "artists": [{"name": "Shubh"}]}],
        },
    )
    monkeypatch.setattr(server, "_yt", fake)
    monkeypatch.setattr(server, "_library_video_ids", lambda *a, **k: set())

    result = server.recommend_from_song(video_id="seed1", limit=5)
    titles = [s["title"] for s in result["songs"]]

    assert titles, "recommend_from_song returned nothing -- this is the v6 bug"
    assert set(titles) <= {"Brown Munde", "Toxic", "Elevated"}
    assert all(s["videoId"].startswith("sp") for s in result["songs"])


def test_recommend_from_song_notes_unresolvable_graph_candidates(graph_enabled, monkeypatch):
    # Dropped candidates are reported, never silently swallowed -- the same
    # partial-results philosophy as a failed signal.
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    fake = _FakeProvider(
        capabilities=set(),
        track_meta={"seed1": {"videoId": "seed1", "title": "Excuses",
                              "artists": [{"name": "AP Dhillon"}]}},
        search_results={},  # nothing resolves
    )
    monkeypatch.setattr(server, "_yt", fake)
    monkeypatch.setattr(server, "_library_video_ids", lambda *a, **k: set())

    result = server.recommend_from_song(video_id="seed1", limit=5)
    assert result["songs"] == []
    assert any("music-graph candidate" in note for note in result["notes"])


def test_library_exclusion_index_is_built_from_the_store(db, monkeypatch):
    store.upsert_tracks(db, [{"videoId": "v1", "title": "Brown Munde", "artists": ["AP Dhillon"]}])
    store.sync_library(db, [("v1", "Liked Music", True)])
    monkeypatch.setattr(server, "_store_conn", db)

    index = server._library_exclusion_index()
    assert match.matches_any("Brown Munde", "AP Dhillon", index)


def test_an_already_joined_artist_credit_is_not_split_into_characters(db):
    # Strings are iterable, so this used to store
    # "A & P &   & D & h & i & l & l & o & n" -- which would then silently fail
    # to match anything in the exclusion index.
    store.upsert_tracks(db, [{"videoId": "v1", "title": "Brown Munde", "artists": "AP Dhillon"}])
    assert store.get_track(db, "v1")["artists"] == "AP Dhillon"


def test_library_exclusion_index_is_empty_rather_than_fatal_without_a_store(monkeypatch):
    # It is an optimisation; stage two is the guarantee. An unavailable store
    # must never fail a recommendation.
    def boom():
        raise RuntimeError("no store")

    monkeypatch.setattr(server, "_store", boom)
    assert server._library_exclusion_index() == {}


def test_lyrics_degrade_on_a_backend_that_has_none(db):
    """Absence of a capability must degrade, not raise.

    v6 moved scripts/label_library.py onto the Provider seam, so lyrics.fetch
    now receives a Provider rather than a raw ytmusicapi client. Spotify
    exposes no lyrics at all; that has to look like "this song has no lyrics",
    not an AttributeError halfway through a labelling run.
    """
    import lyrics

    class _NoLyrics:
        def get_watch_playlist(self, videoId, limit=1, radio=False):
            raise AssertionError("must not even ask when the backend has no lyrics")

    assert lyrics.fetch(_NoLyrics(), "v1") == (None, None)


def test_lyrics_treat_a_provider_failure_as_a_missing_lyric(db):
    # A transient provider failure should cost one unlabelled song, not the
    # whole run -- which is what catching these is for.
    import lyrics

    class _Boom:
        def get_watch_playlist(self, videoId, limit=1, radio=False):
            raise server.ProviderError("rate limited")

        def get_lyrics(self, browseId):
            raise AssertionError("unreachable")

    assert lyrics.fetch(_Boom(), "v1") == (None, None)


def test_index_status_reports_the_graph(graph_enabled, monkeypatch):
    # On a backend with no native signals the graph IS the recommender, so an
    # empty graph is the difference between good results and none.
    monkeypatch.setattr(server, "_yt", _FakeProvider(capabilities=set()))
    status = server._graph_status()
    assert status["enabled"] is True
    assert status["native_signals"] is None, "no native signals is the headline fact"
    assert "cache" in status and "atlas" in status


def test_index_status_reports_native_signals_when_present(graph_enabled, monkeypatch):
    monkeypatch.setattr(server, "_yt", _FakeProvider())
    assert server._graph_status()["native_signals"] == ["artist", "radio", "related"]


def test_index_status_says_so_when_the_graph_is_off(monkeypatch):
    monkeypatch.setattr(server, "GRAPH_ENABLED", False)
    assert server._graph_status() == {"enabled": False}


def test_index_status_never_fails_the_call(graph_enabled, monkeypatch):
    # A status report that raises is worse than one that reports an error.
    def boom():
        raise RuntimeError("graph cache unreadable")

    monkeypatch.setattr(server, "_graph", boom)
    assert "error" in server._graph_status()


def test_graph_can_be_disabled_by_environment(monkeypatch):
    monkeypatch.setattr(server, "GRAPH_ENABLED", False)
    monkeypatch.setattr(server, "_graph_conn_cache", None)
    assert server._graph() is None


# --- v6 follow-up: the mood path was never wired to the graph ----------------


def _mood_library(db):
    """One library song with a Sad label, so pick_seeds has something to use."""
    import moodspace as ms
    store.sync_library(db, [("seed", "Liked Music", True)])
    store.upsert_tracks(db, [{"videoId": "seed", "title": "Excuses", "artists": ["AP Dhillon"]}])
    store.put_track_moods(db, "atlas", [("seed", ms.ANCHORS["Sad"], 0.9)])


def test_mood_recommendations_use_the_graph_on_a_backend_with_no_native_signals(
    db, graph_db, monkeypatch
):
    """The gap this fixes, stated as the failure it actually was.

    v6 wired the graph into `recommend_from_song`/`recommend_from_playlist` and
    not into `recommend.build`, which called `gather_seeds` without
    `graph_conn`. On YouTube that was invisible -- native signals fill the pool.
    Measured on Spotify, where `capabilities()` is `(none)`: 6 seeds resolved
    correctly and 0 candidates came back, so every mood returned 0 songs.
    """
    import graph_atlas
    import moodspace as ms
    import recommend

    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    _mood_library(db)
    # The graph atlas knows a mood for one of the neighbours.
    graph_atlas.record_playlist(
        graph_db, 7, "Sad", "sad songs", "Sad Mix",
        [{"id": 200, "title": "Elevated", "artist_name": "Shubh"}],
    )
    graph_atlas.materialize_moods(graph_db)

    provider_no_signals = _FakeProvider(
        capabilities=set(),
        search_results={"Elevated Shubh": [
            {"videoId": "sp_elevated", "title": "Elevated", "artists": [{"name": "Shubh"}]}
        ]},
    )

    result = recommend.build(
        provider_no_signals, db, exclude={"seed"}, feeling="heartbroken",
        arc="mirror", limit=3, use_history=False, graph_conn=graph_db,
    )

    assert result["songs"], "a backend with no native signals must still get candidates"
    assert [s["videoId"] for s in result["songs"]] == ["sp_elevated"]
    # And it arrives mood-rated, from the graph atlas rather than unrated
    # filler -- which is the whole point on a backend where graph candidates
    # are the only candidates.
    assert result["songs"][0]["mood_source"] == graph_atlas.SOURCE
    assert result["songs"][0]["rated"] is True


def test_a_graph_candidate_never_leaks_its_pool_key_as_a_provider_id(db, graph_db, monkeypatch):
    """`graph:<deezer id>` is not an id any backend can play."""
    import recommend

    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    _mood_library(db)
    # Nothing resolves: every graph candidate should be dropped, not returned
    # under its pool key.
    provider_no_signals = _FakeProvider(capabilities=set(), search_results={})

    result = recommend.build(
        provider_no_signals, db, exclude={"seed"}, feeling="heartbroken",
        arc="mirror", limit=3, use_history=False, graph_conn=graph_db,
    )

    assert result["songs"] == []
    assert any("couldn't be matched" in note for note in result["notes"])


def test_mood_path_excludes_library_songs_that_have_no_provider_id_yet(db, graph_db, monkeypatch):
    """Stage one of exclusion, on the mood path.

    A graph candidate is keyed `graph:<deezer id>` and has no provider id, so
    testing it against the id-keyed exclusion set always passes. Without a
    title/artist check the never-recommend-a-library-song guarantee silently
    stops applying to every graph candidate.
    """
    import match as match_mod
    import recommend

    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    _mood_library(db)
    provider_no_signals = _FakeProvider(
        capabilities=set(),
        search_results={"Elevated Shubh": [
            {"videoId": "sp_elevated", "title": "Elevated", "artists": [{"name": "Shubh"}]}
        ]},
    )

    # The library already contains "Elevated" by Shubh, under a different id.
    index = match_mod.build_index([("Elevated", "Shubh")])
    result = recommend.build(
        provider_no_signals, db, exclude={"seed"}, feeling="heartbroken",
        arc="mirror", limit=3, use_history=False, graph_conn=graph_db,
        exclude_index=index,
    )

    assert "Elevated" not in [s["title"] for s in result["songs"]]
    # And it was dropped before resolution, not after: stage one is also the
    # optimisation that keeps an excluded candidate from costing a search.
    assert "Elevated Shubh" not in provider_no_signals.search_calls


def test_the_graph_is_untouched_when_no_connection_is_passed(db, monkeypatch):
    """v1 behaviour, exactly. Every existing caller relies on this."""
    import recommend

    def explode(_url):
        raise AssertionError("the graph must not be consulted without graph_conn")

    monkeypatch.setattr(graph, "_get", explode)
    _mood_library(db)
    provider_with_radio = _FakeProvider(
        watch={"seed": {"tracks": [
            {"videoId": "native1", "title": "Native", "artists": [{"name": "Someone"}]}
        ]}},
    )
    result = recommend.build(
        provider_with_radio, db, exclude={"seed"}, feeling="heartbroken",
        arc="mirror", limit=1, use_history=False,
    )
    assert [s["videoId"] for s in result["songs"]] == ["native1"]


def test_the_graph_survives_being_used_from_a_worker_thread(graph_db, monkeypatch):
    """`gather_seeds` fans seeds out across a thread pool.

    `sqlite3.threadsafety` is 1: a connection used off its creating thread
    raises ProgrammingError, and `gather_seeds` swallows per-seed exceptions by
    design. So every multi-seed request lost its graph candidates *silently* --
    invisible on YouTube, total on a backend where the graph is the only
    source. This is the regression test for that, and it must stay
    multi-seed: the single-seed path deliberately stays on the calling thread,
    which is why `recommend_from_song` never showed it.
    """
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    fake = _FakeProvider(capabilities=set())

    per_seed = signals.gather_seeds(
        fake, ["seed_a", "seed_b"],
        graph_conn=graph_db,
        seed_meta={
            "seed_a": {"title": "Excuses", "artists": ["AP Dhillon"]},
            "seed_b": {"title": "Excuses", "artists": ["AP Dhillon"]},
        },
    )

    assert len(per_seed) == 2, "a seed must not be lost to a cross-thread error"
    assert all(found for found in per_seed), "each seed must return graph candidates"


def test_a_joined_credit_is_not_read_one_letter_at_a_time():
    """Third instance of the same bug; see match.artist_list.

    The store keeps artists as one joined string. Handed to code expecting a
    list, "AP Dhillon" becomes ["A", "P", ...] and the graph looks up an
    artist called "A".
    """
    assert match.artist_list("AP Dhillon & Gurinder Gill") == ["AP Dhillon", "Gurinder Gill"]
    assert match.artist_list("AP Dhillon") == ["AP Dhillon"]
    assert match.artist_list(["AP Dhillon"]) == ["AP Dhillon"]
    assert match.artist_list(None) == []
    assert match.artist_list("") == []
