"""Unit tests for server.py.

Everything here runs against a hand-rolled fake YTMusic client -- no network,
no headers_auth.json required. This complements (does not replace)
scripts/test_recommend.py, which is a real-account smoke test.
"""

import json
import time

import pytest
from ytmusic_client import YTMusicMCPError

import server
from server import (
    _artist_song_catalog,
    _build_library_video_ids,
    _finalize,
    _gather_seed_candidates,
    _liked_video_ids,
    _library_video_ids,
    _merge_and_score,
    _norm_track,
    _read_cache,
    _recent_liked_video_ids,
    _resolve_artist,
    _resolve_song_video_id,
    _write_cache,
    handle_errors,
    recommend_from_playlist,
    refresh_library,
    recommend_from_song,
    songs_by_artist,
)


# --- _norm_track -------------------------------------------------------


def test_norm_track_artists_list():
    item = {"videoId": "v1", "title": "Song", "artists": [{"name": "A"}, {"name": "B"}]}
    assert _norm_track(item)["artists"] == ["A", "B"]


def test_norm_track_artists_missing_name_is_dropped():
    item = {"videoId": "v1", "title": "Song", "artists": [{"name": "A"}, {"id": "x"}]}
    assert _norm_track(item)["artists"] == ["A"]


def test_norm_track_falls_back_to_artist_string():
    item = {"videoId": "v1", "title": "Song", "artist": "Solo Artist"}
    assert _norm_track(item)["artists"] == ["Solo Artist"]


def test_norm_track_no_artist_info():
    item = {"videoId": "v1", "title": "Song"}
    assert _norm_track(item)["artists"] == []


def test_norm_track_album_dict():
    item = {"videoId": "v1", "title": "Song", "album": {"name": "Album Name", "id": "al1"}}
    assert _norm_track(item)["album"] == "Album Name"


def test_norm_track_album_string():
    item = {"videoId": "v1", "title": "Song", "album": "Album Name"}
    assert _norm_track(item)["album"] == "Album Name"


def test_norm_track_album_missing():
    item = {"videoId": "v1", "title": "Song"}
    assert _norm_track(item)["album"] is None


# --- _merge_and_score ----------------------------------------------------


def test_merge_and_score_single_seed():
    per_seed = [
        {
            "v1": {"videoId": "v1", "title": "T1", "artists": [], "album": None, "sources": {"radio", "related"}},
        }
    ]
    merged = _merge_and_score(per_seed)
    assert merged["v1"]["score"] == 2
    assert merged["v1"]["sources"] == {"radio", "related"}


def test_merge_and_score_combines_across_seeds():
    per_seed = [
        {"v1": {"videoId": "v1", "title": "T1", "artists": [], "album": None, "sources": {"radio", "related"}}},
        {"v1": {"videoId": "v1", "title": "T1", "artists": [], "album": None, "sources": {"artist"}}},
    ]
    merged = _merge_and_score(per_seed)
    assert merged["v1"]["score"] == 3
    assert merged["v1"]["sources"] == {"radio", "related", "artist"}


def test_merge_and_score_keeps_candidates_separate():
    per_seed = [
        {"v1": {"videoId": "v1", "title": "T1", "artists": [], "album": None, "sources": {"radio"}}},
        {"v2": {"videoId": "v2", "title": "T2", "artists": [], "album": None, "sources": {"radio"}}},
    ]
    merged = _merge_and_score(per_seed)
    assert set(merged.keys()) == {"v1", "v2"}


# --- _finalize -------------------------------------------------------------


def _candidate(vid, score, title=None, sources=("radio",)):
    return {
        "videoId": vid, "title": title or f"Song {vid}", "artists": [], "album": None,
        "score": score, "sources": set(sources),
    }


def test_finalize_excludes_ids():
    merged = {"v1": _candidate("v1", 1), "v2": _candidate("v2", 2)}
    out, collapsed = _finalize(merged, exclude={"v1"}, limit=10)
    assert [c["videoId"] for c in out] == ["v2"]
    assert collapsed == 0


def test_finalize_sorts_by_score_desc_then_title_asc():
    merged = {
        "a": _candidate("a", 1, title="Zebra"),
        "b": _candidate("b", 3, title="Apple"),
        "c": _candidate("c", 3, title="Banana"),
    }
    out, _ = _finalize(merged, exclude=set(), limit=10)
    assert [c["videoId"] for c in out] == ["b", "c", "a"]


def test_finalize_respects_limit():
    merged = {str(i): _candidate(str(i), i) for i in range(5)}
    out, _ = _finalize(merged, exclude=set(), limit=2)
    assert len(out) == 2
    assert [c["videoId"] for c in out] == ["4", "3"]


def test_finalize_sources_sorted_in_output():
    merged = {"v1": _candidate("v1", 1, sources=("related", "artist", "radio"))}
    out, _ = _finalize(merged, exclude=set(), limit=10)
    assert out[0]["sources"] == ["artist", "radio", "related"]


def test_finalize_collapses_variants_keeps_higher_score():
    merged = {
        "v1": _candidate("v1", 2, title="Dead and Gone"),
        "v2": _candidate("v2", 5, title="Dead and Gone (feat. Justin Timberlake)"),
    }
    merged["v1"]["artists"] = ["T.I."]
    merged["v2"]["artists"] = ["T.I.", "Justin Timberlake"]
    out, collapsed = _finalize(merged, exclude=set(), limit=10)
    assert [c["videoId"] for c in out] == ["v2"]
    assert collapsed == 1


# --- _liked_video_ids --------------------------------------------------


class _FakeYT:
    def __init__(
        self,
        watch=None,
        related_sections=None,
        artists=None,
        playlists=None,
        search_results=None,
        library_playlists=None,
    ):
        self._watch = watch or {}
        self._related_sections = related_sections or {}
        self._artists = artists or {}
        self._playlists = playlists or {}
        self._search_results = search_results or {}
        self._library_playlists = library_playlists if library_playlists is not None else []
        self.get_song_related_calls = []
        self.get_artist_calls = []
        self.search_calls = []
        self.get_playlist_calls = []
        self.get_playlist_limits = []
        self.get_library_playlists_calls = 0

    def get_watch_playlist(self, videoId, limit=25, radio=True):
        result = self._watch.get(videoId)
        if isinstance(result, Exception):
            raise result
        return result

    def get_song_related(self, browse_id):
        self.get_song_related_calls.append(browse_id)
        result = self._related_sections.get(browse_id)
        if isinstance(result, Exception):
            raise result
        return result or []

    def get_artist(self, artist_id):
        self.get_artist_calls.append(artist_id)
        result = self._artists.get(artist_id)
        if isinstance(result, Exception):
            raise result
        return result

    def get_playlist(self, playlist_id, limit=None):
        self.get_playlist_calls.append(playlist_id)
        self.get_playlist_limits.append(limit)
        result = self._playlists[playlist_id]
        if isinstance(result, Exception):
            raise result
        return result

    def search(self, query, filter=None, limit=20):
        self.search_calls.append((query, filter, limit))
        result = self._search_results.get(query)
        if isinstance(result, Exception):
            raise result
        return result or []

    def get_library_playlists(self, limit=25):
        self.get_library_playlists_calls += 1
        return self._library_playlists


def test_liked_video_ids_filters_missing_ids():
    yt = _FakeYT(playlists={"LM": {"tracks": [{"videoId": "a"}, {"videoId": "b"}, {"videoId": None}, {}]}})
    assert _liked_video_ids(yt) == {"a", "b"}


# --- _library_video_ids --------------------------------------------------


def test_library_video_ids_unions_liked_and_all_playlists():
    yt = _FakeYT(
        playlists={
            "LM": {"tracks": [{"videoId": "liked1"}]},
            "PL1": {"tracks": [{"videoId": "pl1song1"}, {"videoId": "pl1song2"}]},
            "PL2": {"tracks": [{"videoId": "pl2song1"}, {"videoId": None}]},
        },
        library_playlists=[{"playlistId": "PL1"}, {"playlistId": "PL2"}],
    )
    assert _library_video_ids(yt) == {"liked1", "pl1song1", "pl1song2", "pl2song1"}


def test_library_video_ids_skips_lm_entry_in_library_playlists_listing():
    # get_library_playlists() can itself list "LM" -- must not double-fetch it.
    yt = _FakeYT(
        playlists={"LM": {"tracks": [{"videoId": "liked1"}]}},
        library_playlists=[{"playlistId": "LM"}],
    )
    _library_video_ids(yt)
    assert yt.get_playlist_calls.count("LM") == 1


def test_library_video_ids_skips_playlist_that_fails_to_fetch():
    yt = _FakeYT(
        playlists={
            "LM": {"tracks": []},
            "PL1": YTMusicMCPError("gone"),
            "PL2": {"tracks": [{"videoId": "ok1"}]},
        },
        library_playlists=[{"playlistId": "PL1"}, {"playlistId": "PL2"}],
    )
    assert _library_video_ids(yt) == {"ok1"}


# --- _resolve_artist / _artist_song_catalog -------------------------------


def test_resolve_artist_returns_top_match():
    yt = _FakeYT(search_results={"Oasis": [{"artist": "Oasis", "browseId": "UC1"}, {"artist": "Oasis Tribute"}]})
    resolved = _resolve_artist(yt, "Oasis")
    assert resolved == {"artist": "Oasis", "browseId": "UC1"}
    assert yt.search_calls == [("Oasis", "artists", 1)]


def test_resolve_artist_no_match_returns_none():
    yt = _FakeYT(search_results={"Nobody": []})
    assert _resolve_artist(yt, "Nobody") is None


def test_artist_song_catalog_prefers_full_songs_playlist():
    yt = _FakeYT(
        artists={"UC1": {"songs": {"browseId": "VLPL1", "results": [{"videoId": "preview1"}]}}},
        playlists={"VLPL1": {"tracks": [{"videoId": "full1"}, {"videoId": "full2"}]}},
    )
    catalog = _artist_song_catalog(yt, "UC1")
    assert [t["videoId"] for t in catalog] == ["full1", "full2"]


def test_artist_song_catalog_falls_back_to_preview_when_full_fetch_fails():
    yt = _FakeYT(
        artists={"UC1": {"songs": {"browseId": "VLPL1", "results": [{"videoId": "preview1"}]}}},
        playlists={"VLPL1": YTMusicMCPError("gone")},
    )
    catalog = _artist_song_catalog(yt, "UC1")
    assert [t["videoId"] for t in catalog] == ["preview1"]


def test_artist_song_catalog_falls_back_when_no_browse_id():
    yt = _FakeYT(artists={"UC1": {"songs": {"browseId": None, "results": [{"videoId": "preview1"}]}}})
    catalog = _artist_song_catalog(yt, "UC1")
    assert [t["videoId"] for t in catalog] == ["preview1"]


# --- songs_by_artist (integration) ----------------------------------------


def test_songs_by_artist_excludes_liked_and_all_playlists(monkeypatch):
    yt = _FakeYT(
        search_results={"Test Artist": [{"artist": "Test Artist", "browseId": "UC1"}]},
        artists={
            "UC1": {
                "songs": {
                    "browseId": "VLPL1",
                    "results": [],
                }
            }
        },
        playlists={
            "VLPL1": {
                "tracks": [
                    {"videoId": "s1", "title": "Song 1", "artists": [{"name": "Test Artist"}]},
                    {"videoId": "s2", "title": "Song 2 (liked)", "artists": [{"name": "Test Artist"}]},
                    {"videoId": "s3", "title": "Song 3 (in a playlist)", "artists": [{"name": "Test Artist"}]},
                ]
            },
            "LM": {"tracks": [{"videoId": "s2"}]},
            "PL1": {"tracks": [{"videoId": "s3"}]},
        },
        library_playlists=[{"playlistId": "PL1"}],
    )
    monkeypatch.setattr(server, "_client", lambda: yt)

    result = songs_by_artist("Test Artist", limit=10)

    assert result["artist"] == "Test Artist"
    assert result["requested"] == 10
    assert result["found"] == 1
    assert [s["videoId"] for s in result["songs"]] == ["s1"]


def test_songs_by_artist_collapses_remix_variants(monkeypatch):
    yt = _FakeYT(
        search_results={"T.I.": [{"artist": "T.I.", "browseId": "UC1"}]},
        artists={
            "UC1": {
                "songs": {
                    "browseId": "VLPL1",
                    "results": [],
                }
            }
        },
        playlists={
            "VLPL1": {
                "tracks": [
                    {"videoId": "s1", "title": "Dead and Gone", "artists": [{"name": "T.I."}]},
                    {
                        "videoId": "s2",
                        "title": "Dead and Gone (feat. Justin Timberlake)",
                        "artists": [{"name": "T.I."}, {"name": "Justin Timberlake"}],
                    },
                    {"videoId": "s3", "title": "Whatever You Like", "artists": [{"name": "T.I."}]},
                ]
            },
            "LM": {"tracks": []},
        },
        library_playlists=[],
    )
    monkeypatch.setattr(server, "_client", lambda: yt)

    result = songs_by_artist("T.I.", limit=10)

    assert result["found"] == 2
    assert result["variants_collapsed"] == 1
    assert [s["videoId"] for s in result["songs"]] == ["s1", "s3"]


def test_songs_by_artist_reports_shortfall_instead_of_padding(monkeypatch):
    yt = _FakeYT(
        search_results={"Small Artist": [{"artist": "Small Artist", "browseId": "UC1"}]},
        artists={"UC1": {"songs": {"browseId": "VLPL1", "results": []}}},
        playlists={
            "VLPL1": {"tracks": [{"videoId": f"s{i}", "title": f"Song {i}"} for i in range(7)]},
            "LM": {"tracks": []},
        },
        library_playlists=[],
    )
    monkeypatch.setattr(server, "_client", lambda: yt)

    result = songs_by_artist("Small Artist", limit=10)

    assert result["requested"] == 10
    assert result["found"] == 7
    assert len(result["songs"]) == 7


def test_songs_by_artist_respects_limit(monkeypatch):
    yt = _FakeYT(
        search_results={"Big Artist": [{"artist": "Big Artist", "browseId": "UC1"}]},
        artists={"UC1": {"songs": {"browseId": "VLPL1", "results": []}}},
        playlists={
            "VLPL1": {"tracks": [{"videoId": f"s{i}", "title": f"Song {i}"} for i in range(20)]},
            "LM": {"tracks": []},
        },
        library_playlists=[],
    )
    monkeypatch.setattr(server, "_client", lambda: yt)

    result = songs_by_artist("Big Artist", limit=5)

    assert result["requested"] == 5
    assert result["found"] == 5
    assert len(result["songs"]) == 5


def test_songs_by_artist_no_artist_match_returns_zero_found(monkeypatch):
    yt = _FakeYT(search_results={"Nobody": []})
    monkeypatch.setattr(server, "_client", lambda: yt)

    result = songs_by_artist("Nobody", limit=10)

    assert result == {"artist": None, "requested": 10, "found": 0, "variants_collapsed": 0, "songs": []}


# --- _gather_seed_candidates --------------------------------------------


def test_gather_seed_candidates_full_pipeline():
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [
                {"videoId": seed, "title": "Seed Song", "artists": [{"name": "SeedArtist", "id": "artist1"}]},
                {"videoId": "radio1", "title": "Radio Song", "artists": [{"name": "X"}]},
            ],
            "related": "REL_BROWSE",
        }
    }
    related_sections = {
        "REL_BROWSE": [
            {
                "contents": [
                    {"videoId": "related1", "title": "Related Song", "artists": [{"name": "Y"}]},
                    "some artist bio text",  # regression: non-dict items must not crash parsing
                    {"videoId": seed, "title": "Seed Song"},  # seed reappearing must be excluded
                ]
            }
        ]
    }
    artists = {
        "artist1": {
            "songs": {"results": [{"videoId": "artistsong1", "title": "Artist Song", "artists": [{"name": "SeedArtist"}]}]},
            "related": {
                "results": [
                    {"browseId": "relartist1"},
                    {"browseId": "relartist2"},
                    {"browseId": "relartist3"},  # beyond _RELATED_ARTISTS_TO_EXPAND=2, should be ignored
                ]
            },
        },
        "relartist1": {"songs": {"results": [{"videoId": "relsong1", "title": "Rel Artist Song 1"}]}},
        "relartist2": {"songs": {"results": [{"videoId": "relsong2", "title": "Rel Artist Song 2"}]}},
    }
    yt = _FakeYT(watch=watch, related_sections=related_sections, artists=artists)

    found = _gather_seed_candidates(yt, seed)

    assert seed not in found
    assert found["radio1"]["sources"] == {"radio"}
    assert found["related1"]["sources"] == {"related"}
    assert found["artistsong1"]["sources"] == {"artist"}
    assert found["relsong1"]["sources"] == {"artist"}
    assert found["relsong2"]["sources"] == {"artist"}
    assert "relartist3" not in yt.get_artist_calls  # only first 2 related artists expanded


def test_gather_seed_candidates_signal_failure_is_skipped_not_fatal():
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [{"videoId": seed, "title": "Seed", "artists": [{"name": "A", "id": "artist1"}]}],
            "related": "REL_BROWSE",
        }
    }
    related_sections = {"REL_BROWSE": YTMusicMCPError("related signal down")}
    artists = {"artist1": {"songs": {"results": [{"videoId": "artistsong1", "title": "Artist Song"}]}, "related": {"results": []}}}
    yt = _FakeYT(watch=watch, related_sections=related_sections, artists=artists)

    found = _gather_seed_candidates(yt, seed)

    assert "artistsong1" in found  # artist expansion still worked despite related failing
    assert all("related" not in c["sources"] for c in found.values())


def test_gather_seed_candidates_total_watch_failure_returns_empty():
    seed = "seed1"
    yt = _FakeYT(watch={seed: YTMusicMCPError("network down")})
    assert _gather_seed_candidates(yt, seed) == {}


def test_gather_seed_candidates_excludes_seed_from_radio_tracks():
    seed = "seed1"
    watch = {seed: {"tracks": [{"videoId": seed, "title": "Seed"}], "related": None}}
    yt = _FakeYT(watch=watch)
    assert _gather_seed_candidates(yt, seed) == {}


def test_gather_seed_candidates_seed_artist_lookup_fails_radio_still_returned():
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [
                {"videoId": seed, "title": "Seed", "artists": [{"name": "A", "id": "artist1"}]},
                {"videoId": "radio1", "title": "Radio Song"},
            ],
            "related": None,
        }
    }
    yt = _FakeYT(watch=watch, artists={"artist1": YTMusicMCPError("artist page down")})

    found = _gather_seed_candidates(yt, seed)

    assert found["radio1"]["sources"] == {"radio"}
    assert "artistsong1" not in found


def test_gather_seed_candidates_related_artist_without_browse_id_is_skipped():
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [{"videoId": seed, "title": "Seed", "artists": [{"name": "A", "id": "artist1"}]}],
            "related": None,
        }
    }
    artists = {
        "artist1": {
            "songs": {"results": [{"videoId": "artistsong1", "title": "Artist Song"}]},
            "related": {"results": [{"title": "No Browse Id"}]},  # missing "browseId"
        }
    }
    yt = _FakeYT(watch=watch, artists=artists)

    found = _gather_seed_candidates(yt, seed)

    assert "artistsong1" in found
    assert yt.get_artist_calls == ["artist1"]  # never tried to expand the browseId-less related artist


def test_gather_seed_candidates_related_artist_lookup_failure_is_skipped():
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [{"videoId": seed, "title": "Seed", "artists": [{"name": "A", "id": "artist1"}]}],
            "related": None,
        }
    }
    artists = {
        "artist1": {
            "songs": {"results": [{"videoId": "artistsong1", "title": "Artist Song"}]},
            "related": {"results": [{"browseId": "relartist1"}]},
        },
        "relartist1": YTMusicMCPError("related artist page down"),
    }
    yt = _FakeYT(watch=watch, artists=artists)

    found = _gather_seed_candidates(yt, seed)

    assert "artistsong1" in found  # seed artist's own songs still surfaced despite the related-artist failure


# --- handle_errors -------------------------------------------------------


def test_handle_errors_passes_through_success():
    @handle_errors
    def fn():
        return 42

    assert fn() == 42


def test_handle_errors_ytmusic_mcp_error_passes_through_message():
    # ytmusic-mcp already turns auth/rate-limit/gated/network failures into a
    # clear message -- handle_errors just needs to not mangle it.
    @handle_errors
    def fn():
        raise YTMusicMCPError("YouTube Music is rate-limiting requests right now. Wait a bit and try again.")

    with pytest.raises(RuntimeError, match="rate-limiting"):
        fn()


def test_handle_errors_value_error():
    @handle_errors
    def fn():
        raise ValueError("unknown arc 'sideways'")

    with pytest.raises(RuntimeError, match="unknown arc"):
        fn()


# --- _resolve_song_video_id -----------------------------------------------


def test_resolve_song_video_id_prefers_artist_match():
    yt = _FakeYT(
        search_results={
            "Kryptonite 3 Doors Down": [
                {"videoId": "cover1", "title": "Kryptonite", "artists": [{"name": "Cover Band"}]},
                {"videoId": "orig1", "title": "Kryptonite", "artists": [{"name": "3 Doors Down"}]},
            ]
        }
    )
    assert _resolve_song_video_id(yt, "Kryptonite", "3 Doors Down") == "orig1"


def test_resolve_song_video_id_falls_back_to_top_hit_without_artist_match():
    yt = _FakeYT(search_results={"Some Song": [{"videoId": "top1", "title": "Some Song", "artists": []}]})
    assert _resolve_song_video_id(yt, "Some Song") == "top1"


def test_resolve_song_video_id_no_results_returns_none():
    yt = _FakeYT(search_results={"Nothing Matches": []})
    assert _resolve_song_video_id(yt, "Nothing Matches") is None


# --- recommend_from_song / recommend_from_playlist (integration) --------


def test_recommend_from_song_resolves_seed_from_song_and_artist(monkeypatch):
    watch = {
        "orig1": {
            "tracks": [
                {"videoId": "orig1", "title": "Kryptonite"},
                {"videoId": "cand1", "title": "Candidate 1", "artists": [{"name": "A"}]},
            ],
            "related": None,
        }
    }
    yt = _FakeYT(
        watch=watch,
        search_results={
            "Kryptonite 3 Doors Down": [{"videoId": "orig1", "title": "Kryptonite", "artists": [{"name": "3 Doors Down"}]}]
        },
        playlists={"LM": {"tracks": []}},
    )
    monkeypatch.setattr(server, "_client", lambda: yt)

    results = recommend_from_song(song="Kryptonite", artist="3 Doors Down", limit=20)

    assert [r["videoId"] for r in results["songs"]] == ["cand1"]


def test_recommend_from_song_no_match_raises(monkeypatch):
    yt = _FakeYT(search_results={"Totally Fake Song": []})
    monkeypatch.setattr(server, "_client", lambda: yt)

    with pytest.raises(RuntimeError, match="No song found"):
        recommend_from_song(song="Totally Fake Song")


def test_recommend_from_song_requires_video_id_or_song(monkeypatch):
    yt = _FakeYT()
    monkeypatch.setattr(server, "_client", lambda: yt)

    with pytest.raises(RuntimeError, match="Provide either"):
        recommend_from_song()


def test_recommend_from_song_excludes_liked(monkeypatch):
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [
                {"videoId": seed, "title": "Seed"},
                {"videoId": "cand1", "title": "Candidate 1", "artists": [{"name": "A"}]},
                {"videoId": "liked1", "title": "Already Liked", "artists": [{"name": "B"}]},
            ],
            "related": None,
        }
    }
    yt = _FakeYT(watch=watch, playlists={"LM": {"tracks": [{"videoId": "liked1"}]}})
    monkeypatch.setattr(server, "_client", lambda: yt)

    results = recommend_from_song(seed, limit=20)

    ids = [r["videoId"] for r in results["songs"]]
    assert "cand1" in ids
    assert "liked1" not in ids
    assert seed not in ids


def test_recommend_from_song_excludes_songs_in_any_playlist_not_just_liked(monkeypatch):
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [
                {"videoId": seed, "title": "Seed"},
                {"videoId": "cand1", "title": "Candidate 1", "artists": [{"name": "A"}]},
                {"videoId": "inplaylist1", "title": "Already In A Playlist", "artists": [{"name": "B"}]},
            ],
            "related": None,
        }
    }
    yt = _FakeYT(
        watch=watch,
        playlists={"LM": {"tracks": []}, "PL1": {"tracks": [{"videoId": "inplaylist1"}]}},
        library_playlists=[{"playlistId": "PL1"}],
    )
    monkeypatch.setattr(server, "_client", lambda: yt)

    results = recommend_from_song(seed, limit=20)

    ids = [r["videoId"] for r in results["songs"]]
    assert "cand1" in ids
    assert "inplaylist1" not in ids


def test_recommend_from_song_same_artist_only_filters_other_artists(monkeypatch):
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [
                {"videoId": seed, "title": "Where Are You Now", "artists": [{"name": "Lost Frequencies", "id": "artist1"}]},
                {"videoId": "other1", "title": "Other Artist Song", "artists": [{"name": "Someone Else"}]},
            ],
            "related": None,
        }
    }
    artists = {
        "artist1": {
            "songs": {"results": [{"videoId": "lf1", "title": "LF Song", "artists": [{"name": "Lost Frequencies"}]}]},
            "related": {"results": []},
        }
    }
    yt = _FakeYT(watch=watch, artists=artists, playlists={"LM": {"tracks": []}})
    monkeypatch.setattr(server, "_client", lambda: yt)

    results = recommend_from_song(seed, limit=20, same_artist_only=True)

    ids = [r["videoId"] for r in results["songs"]]
    assert ids == ["lf1"]
    assert "other1" not in ids


def test_recommend_from_song_same_artist_only_uses_artist_param_when_seed_lookup_lacks_artists(monkeypatch):
    watch = {
        "orig1": {
            "tracks": [
                {"videoId": "orig1", "title": "Kryptonite"},
                {"videoId": "cand1", "title": "Candidate", "artists": [{"name": "3 Doors Down"}]},
                {"videoId": "cand2", "title": "Unrelated", "artists": [{"name": "Someone Else"}]},
            ],
            "related": None,
        }
    }
    yt = _FakeYT(
        watch=watch,
        search_results={
            "Kryptonite 3 Doors Down": [{"videoId": "orig1", "title": "Kryptonite", "artists": [{"name": "3 Doors Down"}]}]
        },
        playlists={"LM": {"tracks": []}},
    )
    monkeypatch.setattr(server, "_client", lambda: yt)

    results = recommend_from_song(song="Kryptonite", artist="3 Doors Down", limit=20, same_artist_only=True)

    assert [r["videoId"] for r in results["songs"]] == ["cand1"]


def test_recommend_from_song_same_artist_only_false_by_default(monkeypatch):
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [
                {"videoId": seed, "title": "Seed", "artists": [{"name": "Lost Frequencies", "id": "artist1"}]},
                {"videoId": "other1", "title": "Other Artist Song", "artists": [{"name": "Someone Else"}]},
            ],
            "related": None,
        }
    }
    yt = _FakeYT(watch=watch, artists={"artist1": {"songs": {"results": []}, "related": {"results": []}}}, playlists={"LM": {"tracks": []}})
    monkeypatch.setattr(server, "_client", lambda: yt)

    results = recommend_from_song(seed, limit=20)

    assert [r["videoId"] for r in results["songs"]] == ["other1"]


def test_recommend_from_playlist_excludes_seed_playlist_and_liked(monkeypatch):
    tracks = [{"videoId": "t1"}, {"videoId": "t2"}]
    watch = {
        "t1": {"tracks": [{"videoId": "t1"}, {"videoId": "cand1", "title": "C1", "artists": []}], "related": None},
        "t2": {"tracks": [{"videoId": "t2"}, {"videoId": "t1", "title": "T1 again"}], "related": None},
    }
    yt = _FakeYT(
        watch=watch,
        playlists={"PL1": {"tracks": tracks}, "LM": {"tracks": [{"videoId": "liked1"}]}},
    )
    monkeypatch.setattr(server, "_client", lambda: yt)

    results = recommend_from_playlist("PL1", limit=20, seed_sample_size=5)

    ids = {r["videoId"] for r in results}
    assert ids == {"cand1"}  # t1/t2 excluded as seed-playlist members, seeds sampled == full playlist here


def test_recommend_from_playlist_excludes_songs_in_other_playlists(monkeypatch):
    tracks = [{"videoId": "t1"}]
    watch = {
        "t1": {
            "tracks": [
                {"videoId": "t1"},
                {"videoId": "cand1", "title": "C1", "artists": []},
                {"videoId": "otherplaylist1", "title": "In Another Playlist", "artists": []},
            ],
            "related": None,
        }
    }
    yt = _FakeYT(
        watch=watch,
        playlists={
            "PL1": {"tracks": tracks},
            "LM": {"tracks": []},
            "PL2": {"tracks": [{"videoId": "otherplaylist1"}]},
        },
        library_playlists=[{"playlistId": "PL1"}, {"playlistId": "PL2"}],
    )
    monkeypatch.setattr(server, "_client", lambda: yt)

    results = recommend_from_playlist("PL1", limit=20, seed_sample_size=5)

    ids = {r["videoId"] for r in results}
    assert ids == {"cand1"}


def test_recommend_from_playlist_says_why_it_cannot_read_a_playlist(monkeypatch):
    """An unreadable playlist must not look like an exhausted one.

    This returned a bare [] -- indistinguishable from "nothing new to
    recommend here". Measured on Spotify, where the post-Nov-2024 restriction
    403s every playlist read, that meant an empty answer in 1.5s with no
    reason given. `recommend_from_playlist_for_mood` already raised on the
    same condition; the two now give one answer, not two.
    """
    yt = _FakeYT(playlists={"PL1": {"tracks": []}, "LM": {"tracks": []}})
    monkeypatch.setattr(server, "_client", lambda: yt)

    with pytest.raises(RuntimeError, match="no playable tracks"):
        recommend_from_playlist("PL1")


def test_recommend_from_playlist_samples_when_over_seed_size(monkeypatch):
    tracks = [{"videoId": f"t{i}"} for i in range(10)]
    watch = {f"t{i}": {"tracks": [{"videoId": f"t{i}"}], "related": None} for i in range(10)}
    yt = _FakeYT(watch=watch, playlists={"PL1": {"tracks": tracks}, "LM": {"tracks": []}})
    monkeypatch.setattr(server, "_client", lambda: yt)
    monkeypatch.setattr(server.random, "sample", lambda population, k: population[:k])

    recommend_from_playlist("PL1", seed_sample_size=3)

    # only the first 3 tracks should have been used as seeds
    seeded_ids = {t["videoId"] for t in tracks[:3]}
    assert seeded_ids == {"t0", "t1", "t2"}


# --- library exclusion cache ------------------------------------------------


def _cache_fake(liked=("liked1",), playlist_tracks=("pl1song1",)):
    """A fake whose library is one liked song plus one one-song playlist."""
    return _FakeYT(
        playlists={
            "LM": {"tracks": [{"videoId": v} for v in liked]},
            "PL1": {"tracks": [{"videoId": v} for v in playlist_tracks]},
        },
        library_playlists=[{"playlistId": "PL1"}],
    )


def test_library_cache_miss_builds_fully_and_writes_cache(isolated_cache):
    yt = _cache_fake()
    assert _library_video_ids(yt) == {"liked1", "pl1song1"}
    assert yt.get_library_playlists_calls == 1

    written = json.loads(isolated_cache.read_text())
    assert sorted(written["video_ids"]) == ["liked1", "pl1song1"]
    assert written["fetched_at"] <= time.time()


def test_library_cache_hit_skips_the_expensive_playlist_walk():
    # The whole point of the cache: a hit must not re-walk every playlist.
    yt = _cache_fake()
    _library_video_ids(yt)
    yt.get_library_playlists_calls = 0
    yt.get_playlist_calls.clear()

    assert _library_video_ids(yt) == {"liked1", "pl1song1"}
    assert yt.get_library_playlists_calls == 0
    assert yt.get_playlist_calls == ["LM"]  # recent-likes top-up only


def test_library_cache_hit_tops_up_with_recently_liked_songs():
    # A song liked after the cache was built must still be excluded, without
    # waiting out the TTL -- that's what keeps the novelty guarantee honest.
    yt = _cache_fake()
    _library_video_ids(yt)
    yt._playlists["LM"]["tracks"].append({"videoId": "just_liked"})

    assert "just_liked" in _library_video_ids(yt)


def test_library_cache_top_up_uses_a_bounded_fetch_not_the_whole_playlist():
    yt = _cache_fake()
    _library_video_ids(yt)
    yt.get_playlist_limits.clear()

    _library_video_ids(yt)
    assert yt.get_playlist_limits == [server.RECENT_LIKES_LIMIT]


def test_library_cache_top_up_failure_falls_back_to_cached_ids():
    yt = _cache_fake()
    _library_video_ids(yt)
    yt._playlists["LM"] = YTMusicMCPError("transient")

    # Degraded, not broken: a slightly older set beats no recommendation.
    assert _library_video_ids(yt) == {"liked1", "pl1song1"}


def test_library_cache_expired_triggers_rebuild(isolated_cache):
    yt = _cache_fake()
    _library_video_ids(yt)
    stale = json.loads(isolated_cache.read_text())
    stale["fetched_at"] = time.time() - server.CACHE_TTL - 1
    isolated_cache.write_text(json.dumps(stale))
    yt.get_library_playlists_calls = 0

    _library_video_ids(yt)
    assert yt.get_library_playlists_calls == 1


def test_library_cache_disabled_when_ttl_not_positive(monkeypatch):
    monkeypatch.setattr(server, "CACHE_TTL", 0)
    yt = _cache_fake()
    _library_video_ids(yt)
    _library_video_ids(yt)
    assert yt.get_library_playlists_calls == 2


def test_force_refresh_bypasses_a_fresh_cache():
    yt = _cache_fake()
    _library_video_ids(yt)
    yt.get_library_playlists_calls = 0

    _library_video_ids(yt, force_refresh=True)
    assert yt.get_library_playlists_calls == 1


@pytest.mark.parametrize(
    "contents",
    ["not json at all", '{"video_ids": ["a"]}', '{"fetched_at": "nope", "video_ids": []}'],
)
def test_corrupt_or_incomplete_cache_is_a_miss_not_an_error(isolated_cache, contents):
    isolated_cache.write_text(contents)
    assert _read_cache() is None

    yt = _cache_fake()
    assert _library_video_ids(yt) == {"liked1", "pl1song1"}


def test_missing_cache_file_is_a_miss():
    assert _read_cache() is None


def test_non_string_ids_in_cache_are_dropped(isolated_cache):
    isolated_cache.write_text(json.dumps({"fetched_at": time.time(), "video_ids": ["ok", None, 7]}))
    ids, _ = _read_cache()
    assert ids == {"ok"}


def test_write_cache_failure_is_non_fatal(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setattr(server, "CACHE_PATH", blocker / "library_cache.json")

    _write_cache({"a"})  # must not raise
    yt = _cache_fake()
    assert _library_video_ids(yt) == {"liked1", "pl1song1"}


def test_recent_liked_video_ids_filters_missing_ids():
    yt = _FakeYT(playlists={"LM": {"tracks": [{"videoId": "a"}, {"videoId": None}, {}]}})
    assert _recent_liked_video_ids(yt) == {"a"}


def test_build_library_video_ids_ignores_the_cache_entirely(isolated_cache):
    isolated_cache.write_text(json.dumps({"fetched_at": time.time(), "video_ids": ["stale_only"]}))
    yt = _cache_fake()
    assert _build_library_video_ids(yt) == {"liked1", "pl1song1"}


def test_refresh_library_rebuilds_and_reports(monkeypatch, isolated_cache):
    isolated_cache.write_text(json.dumps({"fetched_at": time.time(), "video_ids": ["stale_only"]}))
    yt = _cache_fake()
    monkeypatch.setattr(server, "_client", lambda: yt)

    result = refresh_library()
    assert result["tracks_excluded"] == 2
    assert result["ttl_seconds"] == server.CACHE_TTL
    assert yt.get_library_playlists_calls == 1
    assert sorted(json.loads(isolated_cache.read_text())["video_ids"]) == ["liked1", "pl1song1"]
