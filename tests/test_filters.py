"""Unit tests for tempo, genre/language taxonomy, and the result filters.

No network: Deezer is faked at the transport boundary, and YouTube's genre
pages are replayed from a captured response shape.
"""

import pytest

import filters
import graph
import store
import taxonomy
import tempo


# --- tempo ------------------------------------------------------------------


def test_relative_distance_treats_half_and_double_time_as_close():
    # 170bpm drum-and-bass and 85bpm hip-hop share a pulse; calling them
    # maximally far apart would be musically wrong.
    assert tempo.relative_distance(170, 85) == pytest.approx(0.0)
    assert tempo.relative_distance(60, 120) == pytest.approx(0.0)


def test_relative_distance_separates_genuinely_different_tempos():
    assert tempo.relative_distance(128, 90) > 0.25


def test_similarity_is_none_when_either_tempo_is_unknown():
    # None must stay distinct from 0.0: "we don't know" is not "definitely wrong".
    assert tempo.similarity(None, 120) is None
    assert tempo.similarity(120, None) is None
    assert tempo.similarity(120, 0) is None


def test_similarity_is_one_for_identical_tempo():
    assert tempo.similarity(120, 120) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "bpm,low,high,expected",
    [(100, 90, 110, True), (80, 90, 110, False), (120, 90, 110, False),
     (100, None, 110, True), (100, 90, None, True), (None, 90, 110, None)],
)
def test_in_range(bpm, low, high, expected):
    assert tempo.in_range(bpm, low, high) is expected


class _FakeDeezer:
    """Stands in for graph._get.

    The Deezer transport moved from tempo.py to graph.py in v6 -- Deezer became
    the music graph rather than only a tempo source, so one HTTP client serves
    both. These tests still fake the same boundary; it just lives one module
    over now.
    """

    def __init__(self, results=None, tracks=None, fail=False):
        self.results = results if results is not None else []
        self.tracks = tracks or {}
        self.fail = fail
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("network down")
        if "/search" in url:
            return {"data": self.results}
        track_id = int(url.rsplit("/", 1)[-1])
        return self.tracks[track_id]


def _wire(monkeypatch, fake):
    monkeypatch.setattr(graph, "_get", fake)
    return fake


def test_lookup_returns_the_first_result_carrying_a_tempo(monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer(
        results=[{"id": 1, "artist": {"name": "X"}}, {"id": 2, "artist": {"name": "X"}}],
        tracks={1: {"bpm": 0}, 2: {"bpm": 128.0}},
    ))
    assert tempo.lookup("T", "X", sleep=lambda _s: None) == (128.0, tempo.STATUS_OK, 2)


def test_lookup_reports_no_bpm_when_the_song_exists_without_one(monkeypatch):
    # Measured reality: Deezer has the right song for much of the Punjabi and
    # Hindi catalogue but no tempo analysis for it.
    _wire(monkeypatch, _FakeDeezer(results=[{"id": 1, "artist": {"name": "X"}}], tracks={1: {"bpm": 0}}))
    bpm, status, deezer_id = tempo.lookup("Brown Munde", "X", sleep=lambda _s: None)
    assert (bpm, status, deezer_id) == (None, tempo.STATUS_NO_BPM, 1)


def test_lookup_reports_no_match_on_an_empty_search(monkeypatch):
    _wire(monkeypatch, _FakeDeezer(results=[]))
    assert tempo.lookup("nonexistent", None, sleep=lambda _s: None)[1] == tempo.STATUS_NO_MATCH


def test_lookup_survives_a_network_failure(monkeypatch):
    _wire(monkeypatch, _FakeDeezer(fail=True))
    assert tempo.lookup("T", "X", sleep=lambda _s: None)[1] == tempo.STATUS_NO_MATCH


def test_lookup_without_a_title_makes_no_request(monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer())
    assert tempo.lookup("", None)[1] == tempo.STATUS_NO_MATCH
    assert fake.calls == []


def test_lookup_skips_results_credited_to_a_different_artist(monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer(
        results=[{"id": 1, "artist": {"name": "Someone Else"}}, {"id": 2, "artist": {"name": "3 Doors Down"}}],
        tracks={2: {"bpm": 98.9}},
    ))
    assert tempo.lookup("Kryptonite", "3 Doors Down", sleep=lambda _s: None)[0] == pytest.approx(98.9)
    assert not any(url.endswith("/track/1") for url in fake.calls)


def test_tempo_is_fetched_once_then_cached(db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer(results=[{"id": 1, "artist": {"name": "X"}}], tracks={1: {"bpm": 100.0}}))
    assert tempo.get_or_fetch(db, "v1", "T", "X", sleep=lambda _s: None) == 100.0
    before = len(fake.calls)
    assert tempo.get_or_fetch(db, "v1", "T", "X", sleep=lambda _s: None) == 100.0
    assert len(fake.calls) == before


def test_a_missing_tempo_is_cached_too(db, monkeypatch):
    fake = _wire(monkeypatch, _FakeDeezer(results=[{"id": 1, "artist": {"name": "X"}}], tracks={1: {"bpm": 0}}))
    assert tempo.get_or_fetch(db, "v1", "T", "X", sleep=lambda _s: None) is None
    before = len(fake.calls)
    assert tempo.get_or_fetch(db, "v1", "T", "X", sleep=lambda _s: None) is None
    assert len(fake.calls) == before
    assert store.get_tempo(db, "v1")["status"] == tempo.STATUS_NO_BPM


def test_backfill_skips_songs_already_attempted(db, monkeypatch):
    _wire(monkeypatch, _FakeDeezer(results=[{"id": 1, "artist": {"name": "X"}}], tracks={1: {"bpm": 100.0}}))
    rows = [{"video_id": "v1", "title": "T", "artists": "X"}]
    assert tempo.backfill(db, rows, sleep=lambda _s: None)["resolved"] == 1
    assert tempo.backfill(db, rows, sleep=lambda _s: None)["cached"] == 1


def test_get_tempos_returns_only_known_values(db):
    store.put_tempo(db, "a", 120.0, tempo.STATUS_OK)
    store.put_tempo(db, "b", None, tempo.STATUS_NO_BPM)
    assert store.get_tempos(db, ["a", "b", "c"]) == {"a": 120.0}


def test_tempo_stats_reports_coverage(db):
    store.put_tempo(db, "a", 120.0, tempo.STATUS_OK)
    store.put_tempo(db, "b", None, tempo.STATUS_NO_BPM)
    assert store.tempo_stats(db)["coverage"] == 0.5


# --- taxonomy: script detection --------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [("ਬ੍ਰਾਊਨ ਮੁੰਡੇ", "punjabi"), ("सुन रहा है", "hindi"), ("찬가", "korean"),
     ("ひらがな", "japanese"), ("Brown Munde", None), ("", None), (None, None)],
)
def test_script_language(text, expected):
    assert taxonomy.script_language(text) == expected


def test_script_language_picks_the_dominant_script():
    assert taxonomy.script_language("Remix ਬ੍ਰਾਊਨ ਮੁੰਡੇ ਦਾ") == "punjabi"


# --- taxonomy: parsing YouTube's genre pages -------------------------------


def _song_item(video_id, title, artist_cell):
    return {
        "musicResponsiveListItemRenderer": {
            "playlistItemData": {"videoId": video_id},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": title}]}}},
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": artist_cell}]}}},
            ],
        }
    }


def _playlist_item(browse_id):
    return {
        "musicTwoRowItemRenderer": {
            "title": {"runs": [{"navigationEndpoint": {"browseEndpoint": {"browseId": browse_id}}}]}
        }
    }


class _GenreYT:
    def __init__(self, sections, categories=None):
        self._sections = sections
        self._categories = categories or {"Genres": [{"title": "Bollywood & Indian", "params": "p1"}]}
        self.playlists = {}

    def _send_request(self, endpoint, body):
        return {"contents": {"singleColumnBrowseResultsRenderer": {"tabs": [
            {"tabRenderer": {"content": {"sectionListRenderer": {"contents": self._sections}}}}
        ]}}}

    def get_mood_categories(self):
        return self._categories

    def get_playlist(self, playlist_id, limit=None):
        return self.playlists[playlist_id]


def test_genre_page_extracts_songs_and_playlists():
    # ytmusicapi raises KeyError on 25 of 27 genre categories because these
    # pages lead with song items where its parser expects playlists.
    yt = _GenreYT([
        {"musicCarouselShelfRenderer": {"contents": [_song_item("v1", "Low Fade", "Karan Aujla • 9.3M views")]}},
        {"musicCarouselShelfRenderer": {"contents": [_playlist_item("VLPL123"), _playlist_item("MPREalbum")]}},
    ])
    page = taxonomy.genre_page(yt, "p1")
    assert page["songs"] == [{"videoId": "v1", "title": "Low Fade", "artists": ["Karan Aujla"]}]
    assert page["playlists"] == ["PL123"]  # the album id is not a playlist


def test_genre_page_survives_an_unexpected_shape():
    class _Broken:
        def _send_request(self, endpoint, body):
            return {"contents": {}}

    assert taxonomy.genre_page(_Broken(), "p1") == {"songs": [], "playlists": []}


def test_crawl_genres_records_songs_and_playlist_tracks(db):
    yt = _GenreYT([
        {"musicCarouselShelfRenderer": {"contents": [_song_item("v1", "Song", "Artist • 1M views")]}},
        {"musicCarouselShelfRenderer": {"contents": [_playlist_item("VLPL1")]}},
    ])
    yt.playlists["PL1"] = {"tracks": [{"videoId": "v2", "title": "Other", "artists": [{"name": "Artist"}]}]}
    stats = taxonomy.crawl_genres(yt, db, sleep=lambda _s: None)
    assert stats["genres"] == 1 and stats["playlists"] == 1
    assert store.genres_for(db, "v1") == {"Bollywood & Indian": 1}
    assert store.genres_for(db, "v2") == {"Bollywood & Indian": 1}


# --- taxonomy: resolving a language ----------------------------------------


def test_english_genre_evidence_cannot_outvote_a_real_language(db):
    # The bug this exists for: YouTube files Punjabi rap under "Hip-hop", so
    # plain majority voting labelled Sidhu Moose Wala and Karan Aujla English.
    store.upsert_tracks(db, [{"videoId": "v1", "title": "Song", "artists": ["Sidhu Moose Wala"]}])
    store.record_genre(db, "Hip-hop", ["v1"])
    store.record_genre(db, "Bollywood & Indian", ["v1"])
    assert taxonomy.resolve_language(db, "v1")["language"] == "indian"


def test_english_is_used_when_nothing_else_is_evidenced(db):
    store.upsert_tracks(db, [{"videoId": "v1", "title": "Song", "artists": ["Kendrick Lamar"]}])
    store.record_genre(db, "Hip-hop", ["v1"])
    assert taxonomy.resolve_language(db, "v1")["language"] == "english"


def test_the_users_own_playlist_naming_beats_genre_evidence(db):
    store.upsert_tracks(db, [{"videoId": "v1", "title": "Song", "artists": ["A"]}])
    store.record_genre(db, "Hip-hop", ["v1"])
    store.sync_library(db, [("v1", "C - Punjabi", False)])
    resolved = taxonomy.resolve_language(db, "v1")
    assert (resolved["language"], resolved["source"]) == ("punjabi", taxonomy.SOURCE_LIBRARY)


def test_playlist_names_are_matched_loosely(db):
    # "Punjabu" is a real, hand-typed playlist name in this library.
    store.upsert_tracks(db, [{"videoId": "v1", "title": "Song", "artists": ["A"]}])
    store.sync_library(db, [("v1", "Punjabu", False)])
    assert taxonomy.resolve_language(db, "v1")["language"] == "punjabi"


def test_script_evidence_beats_everything(db):
    store.upsert_tracks(db, [{"videoId": "v1", "title": "ਬ੍ਰਾਊਨ ਮੁੰਡੇ", "artists": ["A"]}])
    store.record_genre(db, "Hip-hop", ["v1"])
    store.sync_library(db, [("v1", "C - Bollywood/Hindi", False)])
    resolved = taxonomy.resolve_language(db, "v1")
    assert (resolved["language"], resolved["source"]) == ("punjabi", taxonomy.SOURCE_SCRIPT)


def test_unlabelled_song_resolves_to_nothing(db):
    store.upsert_tracks(db, [{"videoId": "v1", "title": "Song", "artists": ["Nobody"]}])
    assert taxonomy.resolve_language(db, "v1") is None


def test_artist_languages_sum_weights_across_an_artists_catalogue(db):
    store.upsert_tracks(db, [{"videoId": f"v{i}", "title": "S", "artists": ["Sidhu Moose Wala"]} for i in range(6)])
    for i in range(5):
        store.record_genre(db, "Hip-hop", [f"v{i}"])       # five weak English hits
    store.sync_library(db, [("v5", "C - Punjabi", False)])  # one authoritative filing
    assert taxonomy.artist_languages(db)["sidhu moose wala"] == "punjabi"


@pytest.mark.parametrize(
    "language,want,exclude,expected",
    [("english", ["english"], None, True), ("punjabi", ["english"], None, False),
     ("hindi", ["english"], None, False), ("punjabi", ["punjabi"], None, True),
     ("english", None, ["punjabi"], True), ("punjabi", None, ["punjabi"], False),
     (None, ["english"], None, None)],
)
def test_matches(language, want, exclude, expected):
    assert taxonomy.matches(language, want, exclude) is expected


# --- filters ----------------------------------------------------------------


def _candidate(video_id, title, artist, score=1.0):
    return {"videoId": video_id, "title": title, "artists": [artist], "base_score": score}


def _english_and_punjabi(db):
    store.upsert_tracks(db, [
        {"videoId": "eng", "title": "English Song", "artists": ["Kendrick Lamar"]},
        {"videoId": "pun", "title": "Punjabi Song", "artists": ["Karan Aujla"]},
    ])
    store.record_genre(db, "Hip-hop", ["eng"])
    store.record_genre(db, "Bollywood & Indian", ["pun"])


def test_language_filter_is_a_no_op_when_unset(db):
    candidates = [_candidate("a", "T", "X")]
    kept, report = filters.apply_language(db, candidates)
    assert kept == candidates and report["applied"] is False


def test_language_filter_keeps_only_the_requested_language(db):
    _english_and_punjabi(db)
    kept, report = filters.apply_language(
        db, [_candidate("eng", "English Song", "Kendrick Lamar"), _candidate("pun", "Punjabi Song", "Karan Aujla")],
        want=["english"],
    )
    assert [c["videoId"] for c in kept] == ["eng"]
    assert report["dropped_wrong_language"] == 1


def test_unlabelled_candidates_are_dropped_by_default(db):
    # Someone asking for English only wants a guarantee, and an unlabelled
    # candidate from a Punjabi-seeded pool is probably Punjabi.
    _english_and_punjabi(db)
    kept, report = filters.apply_language(
        db, [_candidate("eng", "English Song", "Kendrick Lamar"), _candidate("mystery", "?", "Nobody")],
        want=["english"],
    )
    assert [c["videoId"] for c in kept] == ["eng"]
    assert report["dropped_unlabelled"] == 1


def test_unlabelled_candidates_can_be_kept_explicitly(db):
    _english_and_punjabi(db)
    kept, report = filters.apply_language(
        db, [_candidate("mystery", "?", "Nobody")], want=["english"], allow_unlabelled=True
    )
    assert [c["videoId"] for c in kept] == ["mystery"]
    assert kept[0]["language"] is None and report["dropped_unlabelled"] == 0


def test_language_falls_back_to_the_artists_other_songs(db):
    store.upsert_tracks(db, [{"videoId": f"known{i}", "title": "S", "artists": ["Karan Aujla"]} for i in range(2)])
    store.record_genre(db, "Bollywood & Indian", ["known0", "known1"])
    kept, _ = filters.apply_language(db, [_candidate("brandnew", "New", "Karan Aujla")], want=["english"])
    assert kept == []  # recognised as Indian via the artist, and excluded


def test_tempo_filter_is_a_no_op_when_unset(db):
    candidates = [_candidate("a", "T", "X")]
    kept, report = filters.apply_tempo(db, candidates)
    assert kept == candidates and report["applied"] is False


def test_tempo_filter_scores_by_proximity(db):
    store.put_tempo(db, "close", 122.0, tempo.STATUS_OK)
    store.put_tempo(db, "far", 70.0, tempo.STATUS_OK)
    kept, _ = filters.apply_tempo(
        db, [_candidate("close", "A", "X"), _candidate("far", "B", "Y")],
        target_bpm=120, resolve_missing=False,
    )
    by_id = {c["videoId"]: c for c in kept}
    assert by_id["close"]["base_score"] > by_id["far"]["base_score"]


def test_tempo_filter_drops_songs_outside_a_hard_range(db):
    store.put_tempo(db, "inside", 100.0, tempo.STATUS_OK)
    store.put_tempo(db, "outside", 160.0, tempo.STATUS_OK)
    kept, report = filters.apply_tempo(
        db, [_candidate("inside", "A", "X"), _candidate("outside", "B", "Y")],
        bpm_min=90, bpm_max=110, resolve_missing=False,
    )
    assert [c["videoId"] for c in kept] == ["inside"]
    assert report["dropped_out_of_range"] == 1


def test_unknown_tempo_is_kept_not_dropped(db):
    # Dropping unknown-tempo songs would quietly delete the whole non-English
    # catalogue, where Deezer has almost no BPM data.
    kept, report = filters.apply_tempo(
        db, [_candidate("mystery", "?", "X")], bpm_min=90, bpm_max=110, resolve_missing=False
    )
    assert [c["videoId"] for c in kept] == ["mystery"]
    assert kept[0]["tempo_fit"] is None and report["unknown_tempo_kept"] == 1


def test_tempo_resolution_is_limited_to_a_shortlist(db, monkeypatch):
    calls = []
    monkeypatch.setattr(tempo, "get_or_fetch", lambda *a, **k: calls.append(a[1]) or 120.0)
    candidates = [_candidate(f"v{i}", "T", "X") for i in range(20)]
    filters.apply_tempo(db, candidates, target_bpm=120, shortlist=5)
    assert len(calls) == 5


def test_lookup_falls_back_to_a_title_only_search(monkeypatch):
    # Real YouTube credits like "Billboard Top 100 Hits" match no Deezer artist,
    # which reported 318 library songs as unmatched when most were findable.
    class _ByQuery:
        def __init__(self):
            self.calls = []

        def __call__(self, url):
            self.calls.append(url)
            if "/search" in url:
                # The artist-gated query finds a differently-credited upload.
                return {"data": [{"id": 1, "title": "Trumpets", "artist": {"name": "Sak Noel"}}]}
            return {1: {"bpm": 164.06}}[int(url.rsplit("/", 1)[-1])]

    _wire(monkeypatch, _ByQuery())
    bpm, status, _ = tempo.lookup("Trumpets", "Billboard Top 100 Hits", sleep=lambda _s: None)
    assert (bpm, status) == (164.06, tempo.STATUS_OK)


def test_title_only_fallback_will_not_attach_another_songs_tempo(monkeypatch):
    _wire(monkeypatch, _FakeDeezer(
        results=[{"id": 1, "title": "A Completely Different Song", "artist": {"name": "Someone"}}],
        tracks={1: {"bpm": 140.0}},
    ))
    assert tempo.lookup("Jaane Kyon Log Pyar", "Udit Narayan", sleep=lambda _s: None)[1] == tempo.STATUS_NO_MATCH


def test_a_bare_language_string_is_read_as_one_language(db):
    """`language="english"` must not become a filter for e, n, g, l, i, s, h.

    The tools declare list[str], but they're called by an LLM and a bare string
    is an easy mistake. Iterating it silently produced a filter that matched
    nothing while reporting itself as applied -- a nonsense filter that looks
    like a working one.
    """
    assert taxonomy.as_languages("english") == ["english"]
    assert taxonomy.as_languages(["english"]) == ["english"]
    assert taxonomy.as_languages(None) is None
    assert taxonomy.as_languages("") is None
    assert taxonomy.as_languages([]) is None

    candidates = [{"videoId": "v1", "title": "Song", "artists": ["Kendrick Lamar"]}]
    _, report = filters.apply_language(db, candidates, want="english")
    assert report["want"] == ["english"], "a bare string must not be split into characters"

    _, excl_report = filters.apply_language(db, candidates, exclude="punjabi")
    assert excl_report["exclude"] == ["punjabi"]


def test_title_match_ignores_bracketed_qualifiers():
    assert tempo._same_title("Kamariya (From \"Stree\")", "Kamariya")
    assert not tempo._same_title("Kamariya", "Something Else")
