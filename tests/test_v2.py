"""Unit tests for the v2 mood engine.

Same philosophy as test_server.py: hand-rolled fakes, no network, no
credentials, no touching the developer's real store.
"""

import time

import pytest
from ytmusicapi.exceptions import YTMusicError, YTMusicServerError

from ytmusic_client import YTMusicMCPError

import arc
import atlas
import judge
import label
import lyrics
import moodspace as ms
import recommend
import sense
import store


# --- moodspace -------------------------------------------------------------


def test_clamp_pins_values_to_axis_bounds():
    clamped = ms.clamp({"valence": -5, "energy": 9, "tension": -2, "depth": 0.5})
    assert clamped == {"valence": -1.0, "energy": 1.0, "tension": 0.0, "depth": 0.5}


def test_fit_is_one_for_identical_vectors():
    assert ms.fit(ms.ANCHORS["Sad"], ms.ANCHORS["Sad"]) == 1.0


def test_distance_is_symmetric():
    a, b = ms.ANCHORS["Sad"], ms.ANCHORS["Party"]
    assert ms.distance(a, b) == pytest.approx(ms.distance(b, a))


def test_tension_separates_workout_from_party():
    # The whole reason the third axis exists: these sit at nearly the same
    # valence/energy, and must not be interchangeable.
    party, workout = ms.ANCHORS["Party"], ms.ANCHORS["Workout"]
    assert abs(party["energy"] - workout["energy"]) < 0.15
    assert ms.fit(party, workout) < 0.85


def test_blend_respects_weights():
    blended = ms.blend([(ms.vector(valence=-1.0), 3.0), (ms.vector(valence=1.0), 1.0)])
    assert blended["valence"] == pytest.approx(-0.5)


def test_blend_ignores_non_positive_weights():
    assert ms.blend([(ms.vector(valence=1.0), 0.0)]) is None


def test_blend_of_nothing_is_none():
    assert ms.blend([]) is None


def test_lerp_hits_both_endpoints_and_clamps_t():
    a, b = ms.ANCHORS["Sad"], ms.ANCHORS["Party"]
    assert ms.lerp(a, b, 0.0) == pytest.approx(a)
    assert ms.lerp(a, b, 1.0) == pytest.approx(b)
    assert ms.lerp(a, b, 5.0) == pytest.approx(b)


def test_from_atlas_counts_weights_by_playlist_count():
    vec, _ = ms.from_atlas_counts({"Sad": 9, "Party": 1})
    assert vec["valence"] < 0  # dominated by Sad, not averaged evenly


def test_from_atlas_counts_confidence_saturates():
    _, low = ms.from_atlas_counts({"Chill": 1})
    _, high = ms.from_atlas_counts({"Chill": 6})
    _, capped = ms.from_atlas_counts({"Chill": 600})
    assert low < high == capped == 1.0


def test_from_atlas_counts_ignores_moods_with_no_anchor():
    # Christmas is deliberately unplaceable -- it's an occasion, not a mood.
    assert ms.from_atlas_counts({"Christmas": 12}) is None


def test_is_vector_rejects_partial_and_boolean_values():
    assert ms.is_vector({"valence": 0, "energy": 0, "tension": 0, "depth": 0})
    assert not ms.is_vector({"valence": 0, "energy": 0})
    assert not ms.is_vector({"valence": True, "energy": 0, "tension": 0, "depth": 0})


def test_describe_names_the_closest_anchors():
    assert "Sad" in ms.describe(ms.ANCHORS["Sad"])


# --- arc -------------------------------------------------------------------


def test_mirror_arc_holds_the_starting_mood():
    targets = arc.targets(ms.ANCHORS["Sad"], "mirror", 5)
    assert all(t == pytest.approx(ms.ANCHORS["Sad"]) for t in targets)


def test_lift_arc_starts_where_they_are_and_rises():
    # The iso-principle: never open above the listener's actual mood.
    start = ms.ANCHORS["Sad"]
    targets = arc.targets(start, "lift", 6)
    assert targets[0]["valence"] == pytest.approx(start["valence"])
    assert targets[-1]["valence"] > targets[0]["valence"]
    assert all(b["valence"] >= a["valence"] for a, b in zip(targets, targets[1:]))


def test_settle_arc_descends_in_energy():
    targets = arc.targets(ms.ANCHORS["Party"], "settle", 5)
    assert targets[-1]["energy"] < targets[0]["energy"]


def test_deepen_arc_increases_depth():
    targets = arc.targets(ms.ANCHORS["Chill"], "deepen", 4)
    assert targets[-1]["depth"] > targets[0]["depth"]


def test_hold_arc_peaks_in_the_middle():
    targets = arc.targets(ms.ANCHORS["Workout"], "hold", 5)
    energies = [t["energy"] for t in targets]
    assert energies[2] == max(energies)
    assert energies[0] == pytest.approx(energies[-1])


def test_single_slot_arc_returns_the_start():
    assert arc.targets(ms.ANCHORS["Sad"], "lift", 1) == [dict(ms.ANCHORS["Sad"])]


def test_zero_slots_returns_nothing():
    assert arc.targets(ms.ANCHORS["Sad"], "lift", 0) == []


def test_unknown_arc_is_rejected():
    with pytest.raises(ValueError, match="Unknown arc"):
        arc.targets(ms.ANCHORS["Sad"], "sideways", 3)


def _cand(vid, artist, vector=None, base=1.0):
    return {"videoId": vid, "title": vid, "artists": [artist], "mood": vector, "base_score": base}


def test_sequence_assigns_slot_indices_in_order():
    pool = [_cand(f"v{i}", f"artist{i}") for i in range(4)]
    out = arc.sequence(pool, arc.targets(ms.ANCHORS["Chill"], "mirror", 3))
    assert [s["slot"] for s in out] == [0, 1, 2]


def test_sequence_prefers_the_best_mood_fit_for_each_slot():
    pool = [_cand("sad", "a", ms.ANCHORS["Sad"]), _cand("party", "b", ms.ANCHORS["Party"])]
    out = arc.sequence(pool, arc.targets(ms.ANCHORS["Sad"], "mirror", 1))
    assert out[0]["videoId"] == "sad"


def test_sequence_caps_songs_per_artist():
    pool = [_cand(f"v{i}", "same artist", ms.ANCHORS["Chill"]) for i in range(6)]
    out = arc.sequence(pool, arc.targets(ms.ANCHORS["Chill"], "mirror", 6), max_per_artist=2)
    assert len(out) == 2


def test_sequence_keeps_unrated_candidates_eligible():
    # Most of a discovery pool is unlabelled; excluding it would confine
    # recommendations to the well-documented corner of the catalogue.
    out = arc.sequence([_cand("v1", "a", None)], arc.targets(ms.ANCHORS["Sad"], "mirror", 1))
    assert len(out) == 1
    assert out[0]["rated"] is False
    assert out[0]["mood_fit"] is None


def test_sequence_stops_when_the_pool_runs_out():
    out = arc.sequence([_cand("v1", "a")], arc.targets(ms.ANCHORS["Chill"], "mirror", 5))
    assert len(out) == 1


# --- store -----------------------------------------------------------------


def test_record_playlist_stores_membership_and_checkpoint(db):
    n = store.record_playlist(db, "PL1", "Sad", "Breakups", [{"videoId": "a"}, {"videoId": "b"}])
    assert n == 2
    assert store.crawled_playlist_moods(db) == {("PL1", "Sad")}
    assert store.atlas_mood_counts(db, "a") == {"Sad": 1}


def test_failed_playlist_is_not_treated_as_crawled(db):
    # Otherwise a rate-limited playlist would be skipped forever on resume.
    store.record_playlist(db, "PL1", "Sad", None, [], status="failed")
    assert store.crawled_playlist_moods(db) == set()


def test_artist_names_accepts_raw_and_normalised_shapes(db):
    store.upsert_tracks(db, [{"videoId": "a", "artists": [{"name": "X"}, {"name": "Y"}]}])
    store.upsert_tracks(db, [{"videoId": "b", "artists": ["Z"]}])
    assert store.get_track(db, "a")["artists"] == "X & Y"
    assert store.get_track(db, "b")["artists"] == "Z"


def test_upsert_does_not_erase_known_fields_with_blanks(db):
    store.upsert_tracks(db, [{"videoId": "a", "title": "T", "artists": ["X"]}])
    store.upsert_tracks(db, [{"videoId": "a"}])
    assert store.get_track(db, "a")["artists"] == "X"


def test_put_track_moods_bulk_upserts(db):
    store.put_track_moods(db, "atlas", [("a", ms.ANCHORS["Sad"], 0.5)])
    store.put_track_moods(db, "atlas", [("a", ms.ANCHORS["Party"], 0.9)])
    rows = store.get_track_moods(db, "a")
    assert len(rows) == 1 and rows[0]["confidence"] == 0.9


def test_sync_library_replaces_rather_than_accumulates(db):
    store.sync_library(db, [("a", "Old", False)])
    store.sync_library(db, [("b", "New", True)])
    assert store.library_video_ids(db) == {"b"}


def test_log_history_is_idempotent_within_one_snapshot(db):
    items = [{"videoId": "a", "played": "Today"}]
    store.log_history(db, items, observed_at=100.0)
    store.log_history(db, items, observed_at=100.0)
    assert len(store.recent_history(db)) == 1


def test_recent_history_counts_repeat_snapshots(db):
    for stamp in (100.0, 200.0, 300.0):
        store.log_history(db, [{"videoId": "a"}], observed_at=stamp)
    assert store.recent_history(db)[0]["snapshots"] == 3


def test_rejected_ids_cover_skipped_and_wrong_mood(db):
    store.put_feedback(db, "a", "skipped")
    store.put_feedback(db, "b", "wrong_mood")
    store.put_feedback(db, "c", "loved")
    assert store.rejected_video_ids(db) == {"a", "b"}


# --- label -----------------------------------------------------------------


def test_better_source_wins_outright(db):
    # Averaging a confident lyric reading with a one-tag atlas guess would make
    # the good answer worse.
    store.put_track_moods(db, "atlas", [("a", ms.ANCHORS["Party"], 1.0)])
    store.put_track_moods(db, "llm", [("a", ms.ANCHORS["Sad"], 0.4)])
    resolved = label.resolve(db, "a")
    assert resolved["source"] == "llm"
    assert resolved["vector"]["valence"] == pytest.approx(ms.ANCHORS["Sad"]["valence"])


def test_resolve_many_handles_more_ids_than_sqlite_variables(db):
    ids = [f"v{i}" for i in range(2000)]
    store.put_track_moods(db, "atlas", [(v, ms.ANCHORS["Chill"], 0.5) for v in ids])
    assert len(label.resolve_many(db, ids)) == 2000


def test_resolve_or_derive_materialises_from_raw_membership(db):
    store.record_playlist(db, "PL1", "Sad", None, [{"videoId": "a"}])
    resolved = label.resolve_or_derive(db, ["a"])
    assert resolved["a"]["source"] == "atlas"
    # ...and persists it, so the next call is a plain lookup.
    assert label.resolve_many(db, ["a"])["a"]["source"] == "atlas"


def test_resolve_or_derive_falls_back_to_the_artist_profile(db):
    store.upsert_tracks(db, [{"videoId": f"known{i}", "artists": ["Neoni"]} for i in range(2)])
    store.upsert_tracks(db, [{"videoId": "unknown", "artists": ["Neoni"]}])
    store.put_track_moods(db, "atlas", [(f"known{i}", ms.ANCHORS["Workout"], 0.8) for i in range(2)])
    resolved = label.resolve_or_derive(db, ["unknown"])
    assert resolved["unknown"]["source"] == "artist"


def test_artist_profile_needs_more_than_one_labelled_song(db):
    store.upsert_tracks(db, [{"videoId": "known", "artists": ["Solo"]}, {"videoId": "other", "artists": ["Solo"]}])
    store.put_track_moods(db, "atlas", [("known", ms.ANCHORS["Sad"], 0.8)])
    assert "other" not in label.resolve_or_derive(db, ["other"])


def test_artist_propagation_discounts_confidence(db):
    store.upsert_tracks(db, [{"videoId": f"k{i}", "artists": ["Band"]} for i in range(2)])
    store.upsert_tracks(db, [{"videoId": "gap", "artists": ["Band"]}])
    store.put_track_moods(db, "atlas", [(f"k{i}", ms.ANCHORS["Sad"], 1.0) for i in range(2)])
    assert label.propagate_by_artist(db) == 1
    assert label.resolve(db, "gap")["confidence"] == pytest.approx(label.ARTIST_CONFIDENCE_FACTOR)


def test_propagation_leaves_directly_evidenced_songs_alone(db):
    store.upsert_tracks(db, [{"videoId": f"k{i}", "artists": ["Band"]} for i in range(3)])
    store.put_track_moods(db, "atlas", [(f"k{i}", ms.ANCHORS["Sad"], 1.0) for i in range(3)])
    assert label.propagate_by_artist(db) == 0


def test_primary_artist_takes_the_lead_credit():
    assert label.primary_artist("Joyner Lucas & Jelly Roll") == "joyner lucas"
    assert label.primary_artist(None) is None
    assert label.primary_artist("") is None


def test_genre_prior_reads_the_users_own_playlist_naming(db):
    store.sync_library(db, [("a", "C - Punjabi", False), ("a", "Newest", False)])
    assert label.genre_prior(db, "a") == "Punjabi"


def test_genre_prior_is_none_without_a_genre_playlist(db):
    store.sync_library(db, [("a", "Newest", False)])
    assert label.genre_prior(db, "a") is None


def test_library_coverage_reports_zero_for_an_empty_library(db):
    assert label.library_coverage(db)["coverage"] == 0.0


def test_library_coverage_breaks_down_by_source(db):
    store.sync_library(db, [("a", "Liked Music", True), ("b", "Liked Music", True)])
    store.put_track_moods(db, "atlas", [("a", ms.ANCHORS["Sad"], 0.5)])
    coverage = label.library_coverage(db)
    assert coverage == {"library": 2, "labelled": 1, "coverage": 0.5, "by_source": {"atlas": 1}}


def test_coverage_by_language_separates_the_catalogues(db):
    """The non-English claim needs a number, and one side must not hide in the other.

    PLAN.md refused to cite a coverage number for the non-English catalogue
    because `taxonomy.script_language` finds none of it -- the titles are
    romanised. This split votes with the genre labels instead.
    """
    store.sync_library(db, [
        ("a", "C - Punjabi", False), ("b", "C - Punjabi", False), ("c", "Newest", False),
    ])
    store.upsert_tracks(db, [
        {"videoId": "a", "title": "Brown Munde", "artists": ["AP Dhillon"]},
        {"videoId": "b", "title": "Excuses", "artists": ["AP Dhillon"]},
        {"videoId": "c", "title": "As It Was", "artists": ["Harry Styles"]},
    ])
    store.put_track_moods(db, "graph_atlas", [("a", ms.ANCHORS["Sad"], 0.5)])

    rows = label.library_coverage_by_language(db)
    assert rows["punjabi"]["library"] == 2
    assert rows["punjabi"]["labelled"] == 1
    assert rows["punjabi"]["by_source"] == {"graph_atlas": 1}
    # A track no source could place is its own bucket, not evidence either way.
    assert rows["unknown"] == {"library": 1, "labelled": 0, "by_source": {}, "coverage": 0.0}
    assert sum(r["library"] for r in rows.values()) == 3


def test_coverage_by_language_is_empty_for_an_empty_library(db):
    assert label.library_coverage_by_language(db) == {}


# --- recommend -------------------------------------------------------------


def test_parse_feeling_matches_known_words():
    assert recommend.parse_feeling("i'm so hyped")["energy"] > 0.7


def test_parse_feeling_blends_multiple_hits():
    blended = recommend.parse_feeling("low and nostalgic")
    assert blended["valence"] < 0 and blended["depth"] > 0.7


def test_parse_feeling_returns_none_when_nothing_matches():
    assert recommend.parse_feeling("asdfgh qwerty") is None
    assert recommend.parse_feeling(None) is None


def test_parse_feeling_ignores_punctuation():
    assert recommend.parse_feeling("sad!!!") is not None


def test_explicit_vector_beats_every_other_signal(db):
    target = ms.vector(valence=0.42, energy=0.11)
    resolved = recommend.resolve_target(db, None, feeling="hyped", vector=target, context="Party")
    assert resolved["origin"] == "explicit"
    assert resolved["target"]["valence"] == pytest.approx(0.42)


def test_context_beats_free_text(db):
    resolved = recommend.resolve_target(db, None, feeling="hyped", context="Sleep")
    assert resolved["origin"] == "context"


def test_feeling_is_used_when_no_vector_or_context(db):
    assert recommend.resolve_target(db, None, feeling="heartbroken")["origin"] == "feeling"


def test_malformed_vector_is_ignored_rather_than_trusted(db):
    resolved = recommend.resolve_target(db, None, feeling="hyped", vector={"valence": 0.5})
    assert resolved["origin"] == "feeling"


def test_falls_back_to_time_of_day_with_nothing_to_go_on(db):
    resolved = recommend.resolve_target(db, None)
    assert resolved["origin"] == "time-of-day"
    assert ms.is_vector(resolved["target"])


def test_atlas_neighbours_are_ranked_by_fit_and_bounded(db):
    store.upsert_tracks(db, [{"videoId": "near", "artists": ["A"]}, {"videoId": "far", "artists": ["B"]}])
    store.put_track_moods(db, "atlas", [("near", ms.ANCHORS["Sad"], 1.0), ("far", ms.ANCHORS["Party"], 1.0)])
    found = recommend.atlas_neighbours(db, ms.ANCHORS["Sad"])
    assert [c["videoId"] for c in found] == ["near"]  # Party is outside the window


def test_pick_seeds_spreads_across_artists(db):
    store.sync_library(db, [(f"v{i}", "Liked Music", True) for i in range(4)])
    store.upsert_tracks(db, [{"videoId": f"v{i}", "title": f"t{i}", "artists": ["One Artist"]} for i in range(3)])
    store.upsert_tracks(db, [{"videoId": "v3", "title": "t3", "artists": ["Other"]}])
    store.put_track_moods(db, "atlas", [(f"v{i}", ms.ANCHORS["Sad"], 1.0) for i in range(4)])
    seeds = recommend.pick_seeds(db, ms.ANCHORS["Sad"], count=4)
    assert len(seeds) == 2  # one per artist, not four by the same one


def test_pick_seeds_respects_a_genre_filter(db):
    store.sync_library(db, [("a", "C - Punjabi", False), ("b", "C - Country", False)])
    store.upsert_tracks(db, [{"videoId": "a", "artists": ["A"]}, {"videoId": "b", "artists": ["B"]}])
    store.put_track_moods(db, "atlas", [(v, ms.ANCHORS["Sad"], 1.0) for v in ("a", "b")])
    seeds = recommend.pick_seeds(db, ms.ANCHORS["Sad"], genres=["Punjabi"])
    assert [s["videoId"] for s in seeds] == ["a"]


def test_pick_seeds_on_an_empty_library_returns_nothing(db):
    assert recommend.pick_seeds(db, ms.ANCHORS["Sad"]) == []


# --- atlas crawling --------------------------------------------------------


class _FakeYT:
    def __init__(self, playlists=None, mood_playlists=None, categories=None):
        self._playlists = playlists or {}
        self._mood_playlists = mood_playlists or {}
        self._categories = categories
        self.get_playlist_calls = []

    def get_mood_categories(self):
        if self._categories is not None:
            return self._categories
        return {"Moods & moments": [{"title": "Sad", "params": "p-sad"}]}

    def get_mood_playlists(self, params):
        result = self._mood_playlists.get(params, [])
        if isinstance(result, Exception):
            raise result
        return result

    def get_playlist(self, playlist_id, limit=None):
        self.get_playlist_calls.append(playlist_id)
        result = self._playlists[playlist_id]
        if isinstance(result, Exception):
            raise result
        return result


def _noop_sleep(_seconds):
    return None


def _fake_with_one_sad_playlist(tracks_or_error):
    return _FakeYT(
        mood_playlists={"p-sad": [{"playlistId": "PL1", "title": "Breakups"}]},
        playlists={"PL1": tracks_or_error},
    )


def test_crawl_stores_tracks_and_reports_progress(db):
    yt = _fake_with_one_sad_playlist({"tracks": [{"videoId": "a"}, {"videoId": "b"}]})
    stats = atlas.crawl(yt, db, moods=["Sad"], sleep=_noop_sleep)
    assert stats["ok"] == 1 and stats["tracks"] == 2
    assert store.atlas_video_ids(db) == {"a", "b"}


def test_crawl_resumes_instead_of_recrawling(db):
    yt = _fake_with_one_sad_playlist({"tracks": [{"videoId": "a"}]})
    atlas.crawl(yt, db, moods=["Sad"], sleep=_noop_sleep)
    stats = atlas.crawl(yt, db, moods=["Sad"], sleep=_noop_sleep)
    assert stats["already_crawled"] == 1 and stats["ok"] == 0


def test_crawl_limit_defers_rather_than_marking_done(db):
    yt = _FakeYT(
        mood_playlists={"p-sad": [{"playlistId": f"PL{i}", "title": "t"} for i in range(3)]},
        playlists={f"PL{i}": {"tracks": [{"videoId": f"v{i}"}]} for i in range(3)},
    )
    stats = atlas.crawl(yt, db, moods=["Sad"], sleep=_noop_sleep, limit=1)
    assert stats["ok"] == 1 and stats["deferred"] == 2 and stats["already_crawled"] == 0


def test_failed_playlist_is_retried_on_the_next_run(db):
    yt = _fake_with_one_sad_playlist(YTMusicError("gone"))
    assert atlas.crawl(yt, db, moods=["Sad"], sleep=_noop_sleep)["failed"] == 1
    yt._playlists["PL1"] = {"tracks": [{"videoId": "a"}]}
    assert atlas.crawl(yt, db, moods=["Sad"], sleep=_noop_sleep)["ok"] == 1


def test_rate_limits_are_waited_out_not_treated_as_failure(db):
    class _Flaky(_FakeYT):
        def __init__(self):
            super().__init__(
                mood_playlists={"p-sad": [{"playlistId": "PL1", "title": "t"}]},
                playlists={},
            )
            self.attempts = 0

        def get_playlist(self, playlist_id, limit=None):
            self.attempts += 1
            if self.attempts < 3:
                raise YTMusicServerError("HTTP 429 Too Many Requests")
            return {"tracks": [{"videoId": "a"}]}

    yt = _Flaky()
    waits = []
    assert atlas.crawl(yt, db, moods=["Sad"], sleep=waits.append)["ok"] == 1
    assert yt.attempts == 3
    assert max(waits) >= atlas.RATE_LIMIT_BACKOFF  # backed off, didn't hammer


def test_only_moods_with_an_anchor_are_enumerated(db):
    yt = _FakeYT(
        categories={"Moods & moments": [
            {"title": "Sad", "params": "p-sad"},
            {"title": "Christmas", "params": "p-xmas"},
        ]},
        mood_playlists={
            "p-sad": [{"playlistId": "PL1", "title": "t"}],
            "p-xmas": [{"playlistId": "PLX", "title": "t"}],
        },
    )
    found = atlas.enumerate_playlists(yt, sleep=_noop_sleep)
    assert [entry[1] for entry in found] == ["PL1"]


def test_a_mood_listing_that_fails_is_skipped_not_fatal(db):
    yt = _FakeYT(mood_playlists={"p-sad": YTMusicError("nope")})
    assert atlas.enumerate_playlists(yt, moods=["Sad"], sleep=_noop_sleep) == []


def test_materialize_writes_one_vector_per_song(db):
    store.record_playlist(db, "PL1", "Sad", None, [{"videoId": "a"}])
    store.record_playlist(db, "PL2", "Sad", None, [{"videoId": "a"}])
    assert atlas.materialize_moods(db) == 1
    assert label.resolve(db, "a")["confidence"] == pytest.approx(2 / 6)


# --- lyrics ----------------------------------------------------------------


class _LyricsYT:
    def __init__(self, browse_id="B1", text="line one\nline two", fail=None):
        self._browse_id, self._text, self._fail = browse_id, text, fail
        self.lyric_calls = 0

    def get_watch_playlist(self, videoId, limit=1):
        if self._fail == "watch":
            raise YTMusicError("nope")
        return {"lyrics": self._browse_id}

    def get_lyrics(self, browse_id):
        self.lyric_calls += 1
        if self._fail == "lyrics":
            raise YTMusicError("nope")
        return {"lyrics": self._text, "source": "src"}


def test_lyrics_are_fetched_once_and_cached(db):
    yt = _LyricsYT()
    assert lyrics.get_or_fetch(db, yt, "a") == "line one\nline two"
    assert lyrics.get_or_fetch(db, yt, "a") == "line one\nline two"
    assert yt.lyric_calls == 1


def test_absence_of_lyrics_is_cached_too(db):
    # Otherwise every instrumental costs two API calls on every pass.
    yt = _LyricsYT(browse_id=None)
    assert lyrics.get_or_fetch(db, yt, "a") is None
    assert lyrics.get_or_fetch(db, yt, "a") is None
    assert store.get_lyrics(db, "a")["available"] == 0


def test_lyric_fetch_failures_are_not_fatal(db):
    assert lyrics.get_or_fetch(db, _LyricsYT(fail="watch"), "a") is None
    assert lyrics.get_or_fetch(db, _LyricsYT(fail="lyrics"), "b") is None


def test_excerpt_trims_on_a_line_boundary():
    text = "\n".join(f"line {i}" for i in range(100))
    trimmed = lyrics.excerpt(text, limit=30)
    assert len(trimmed) <= 30 and not trimmed.endswith("lin")


def test_excerpt_passes_short_lyrics_through():
    assert lyrics.excerpt("short") == "short"
    assert lyrics.excerpt(None) is None


# --- sense -----------------------------------------------------------------


class _HistoryYT:
    def __init__(self, items=None, fail=False):
        self._items, self._fail = items or [], fail

    def get_history(self):
        if self._fail:
            raise YTMusicError("history unavailable")
        return self._items


def _seed_history(db, entries, stamp=1000.0):
    """entries: list of (video_id, artist, anchor|None, repeats)."""
    for video_id, artist, anchor, repeats in entries:
        store.upsert_tracks(db, [{"videoId": video_id, "title": video_id.upper(), "artists": [artist]}])
        if anchor:
            store.put_track_moods(db, "atlas", [(video_id, ms.ANCHORS[anchor], 0.9)])
        for i in range(repeats):
            store.log_history(db, [{"videoId": video_id}], observed_at=stamp + i)


def test_read_mood_with_no_history_says_so(db):
    read = sense.read_mood(db)
    assert read["vector"] is None and "No listening history" in read["evidence"][0]


def test_read_mood_blends_the_moods_of_recent_plays(db):
    _seed_history(db, [("a", "A", "Sad", 1), ("b", "B", "Sad", 1)])
    read = sense.read_mood(db)
    assert read["vector"]["valence"] < -0.5


def test_read_mood_flags_a_song_on_repeat(db):
    _seed_history(db, [("a", "A", "Sad", sense.REPEAT_THRESHOLD)])
    assert any("On repeat" in e for e in sense.read_mood(db)["evidence"])


def test_read_mood_flags_a_dominant_artist(db):
    _seed_history(db, [(f"v{i}", "Joyner Lucas", "Sad", 1) for i in range(5)])
    assert any("Joyner Lucas" in e for e in sense.read_mood(db)["evidence"])


def test_read_mood_reports_a_sinking_session(db):
    # Newest first: the recent half is sad, the older half upbeat.
    for i in range(4):
        _seed_history(db, [(f"old{i}", f"A{i}", "Feel good", 1)], stamp=100.0 + i)
    for i in range(4):
        _seed_history(db, [(f"new{i}", f"B{i}", "Sad", 1)], stamp=900.0 + i)
    assert any("sinking" in e for e in sense.read_mood(db)["evidence"])


def test_read_mood_reports_a_lifting_session(db):
    for i in range(4):
        _seed_history(db, [(f"old{i}", f"A{i}", "Sad", 1)], stamp=100.0 + i)
    for i in range(4):
        _seed_history(db, [(f"new{i}", f"B{i}", "Feel good", 1)], stamp=900.0 + i)
    assert any("lifting" in e for e in sense.read_mood(db)["evidence"])


def test_read_mood_with_unlabelled_history_admits_it(db):
    _seed_history(db, [("a", "A", None, 1)])
    read = sense.read_mood(db)
    assert read["vector"] is None
    assert any("mood label" in e for e in read["evidence"])


def test_read_mood_snapshots_first_when_given_a_client(db):
    yt = _HistoryYT([{"videoId": "a", "played": "Today"}])
    store.upsert_tracks(db, [{"videoId": "a", "artists": ["A"]}])
    store.put_track_moods(db, "atlas", [("a", ms.ANCHORS["Chill"], 0.9)])
    assert sense.read_mood(db, yt)["vector"] is not None


def test_a_failing_snapshot_still_returns_a_reading(db):
    # A stale read beats no read; the tool must not die because history broke.
    _seed_history(db, [("a", "A", "Chill", 1)])
    assert sense.read_mood(db, _HistoryYT(fail=True))["vector"] is not None


def test_recency_weights_favour_the_newest_play():
    weights = sense._recency_weights(5)
    assert weights[0] == 1.0
    assert weights[-1] == pytest.approx(sense._MIN_RECENCY_WEIGHT)
    assert weights == sorted(weights, reverse=True)


def test_recency_weights_handle_degenerate_windows():
    assert sense._recency_weights(0) == []
    assert sense._recency_weights(1) == [1.0]


def test_time_of_day_prior_is_a_known_shape():
    prior = sense.time_of_day_prior()
    assert set(prior) == {"label", "energy_bias", "depth_bias"}


# --- judge -----------------------------------------------------------------


class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Response:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)] if text is not None else []
        self.stop_reason = stop_reason


class _FakeClaude:
    def __init__(self, response):
        self._response = response
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _payload(*entries):
    import json as _json

    return _json.dumps({"songs": list(entries)})


def _entry(video_id="a", **overrides):
    base = {"video_id": video_id, "valence": -0.5, "energy": 0.3,
            "tension": 0.2, "depth": 0.8, "confidence": 0.9}
    base.update(overrides)
    return base


def test_judge_parses_a_well_formed_batch():
    client = _FakeClaude(_Response(_payload(_entry("a"))))
    result = judge.label_batch([{"video_id": "a", "title": "T"}], client=client)
    vector, confidence = result["a"]
    assert vector["valence"] == pytest.approx(-0.5) and confidence == pytest.approx(0.9)


def test_judge_clamps_out_of_range_values():
    client = _FakeClaude(_Response(_payload(_entry("a", valence=-9, confidence=5))))
    vector, confidence = judge.label_batch([{"video_id": "a"}], client=client)["a"]
    assert vector["valence"] == -1.0 and confidence == 1.0


def test_judge_drops_malformed_entries_rather_than_inventing_a_vector():
    client = _FakeClaude(_Response(_payload(_entry("good"), {"video_id": "bad", "valence": "x"}, {"valence": 0.1})))
    result = judge.label_batch([{"video_id": "good"}], client=client)
    assert set(result) == {"good"}


def test_judge_returns_nothing_on_a_refusal():
    client = _FakeClaude(_Response(_payload(_entry()), stop_reason="refusal"))
    assert judge.label_batch([{"video_id": "a"}], client=client) == {}


def test_judge_returns_nothing_on_unparseable_output():
    assert judge.label_batch([{"video_id": "a"}], client=_FakeClaude(_Response("not json"))) == {}


def test_judge_returns_nothing_when_the_response_has_no_text():
    assert judge.label_batch([{"video_id": "a"}], client=_FakeClaude(_Response(None))) == {}


def test_judge_skips_the_api_entirely_for_an_empty_batch():
    client = _FakeClaude(_Response(_payload()))
    assert judge.label_batch([], client=client) == {}
    assert client.calls == []


def test_judge_sends_the_configured_model_and_a_json_schema():
    client = _FakeClaude(_Response(_payload(_entry())))
    judge.label_batch([{"video_id": "a"}], client=client)
    sent = client.calls[0]
    assert sent["model"] == judge.MODEL
    assert sent["output_config"]["format"]["type"] == "json_schema"


def test_judge_renders_every_evidence_field_it_is_given():
    rendered = judge._render(
        {"video_id": "a", "title": "T", "artists": "X", "moods": ["Sad"],
         "playlists": ["Breakups"], "lyrics": "some words"}
    )
    for expected in ("video_id: a", "title: T", "artists: X", "Sad", "Breakups", "some words"):
        assert expected in rendered


def test_judge_says_when_lyrics_are_missing():
    assert "lyrics: unavailable" in judge._render({"video_id": "a", "title": "T"})


def test_batches_chunks_without_dropping_anything():
    items = list(range(25))
    chunks = list(judge.batches(items, size=10))
    assert [len(c) for c in chunks] == [10, 10, 5]
    assert [x for c in chunks for x in c] == items


def test_judge_reports_unavailable_without_the_optional_package(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("no anthropic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert judge.available() is False
    with pytest.raises(judge.JudgeUnavailable, match="anthropic"):
        judge._client()


# --- recommend.build (integration) -----------------------------------------


class _BuildYT:
    """Enough of the API surface for one full mood recommendation."""

    def __init__(self, radio):
        self._radio = radio

    def get_watch_playlist(self, videoId, limit=25, radio=True):
        return {"tracks": self._radio.get(videoId, [])}

    def get_history(self):
        return []


def _library(db, *songs):
    """Build a library in one call -- sync_library replaces, so adding songs
    one at a time would keep only the last."""
    store.sync_library(db, [(vid, "Liked Music", True) for vid, _, _ in songs])
    store.upsert_tracks(db, [{"videoId": v, "title": v.upper(), "artists": [a]} for v, a, _ in songs])
    store.put_track_moods(db, "atlas", [(v, ms.ANCHORS[anchor], 0.9) for v, _, anchor in songs])


def test_build_seeds_from_the_library_and_returns_a_sequence(db):
    _library(db, ("seed", "Seeder", "Sad"))
    yt = _BuildYT({"seed": [{"videoId": "new1", "title": "New One", "artists": [{"name": "Fresh"}]}]})

    result = recommend.build(yt, db, exclude={"seed"}, feeling="heartbroken", arc="mirror", limit=1)
    assert result["target_origin"] == "feeling"
    assert [s["videoId"] for s in result["seeds"] if "videoId" in s] or result["seeds"]
    assert [s["videoId"] for s in result["songs"]] == ["new1"]
    assert result["songs"][0]["slot"] == 0


def test_build_never_returns_an_excluded_song(db):
    _library(db, ("seed", "Seeder", "Sad"))
    yt = _BuildYT({"seed": [{"videoId": "owned", "title": "Owned", "artists": [{"name": "X"}]}]})
    result = recommend.build(yt, db, exclude={"seed", "owned"}, feeling="heartbroken", limit=5)
    assert result["songs"] == []
    assert any("exclusion" in n for n in result["notes"])


def test_build_boosts_songs_by_artists_already_in_the_library(db):
    _library(db, ("seed", "Seeder", "Sad"), ("known", "Familiar", "Sad"))
    yt = _BuildYT({
        "seed": [
            {"videoId": "byknown", "title": "A", "artists": [{"name": "Familiar"}]},
            {"videoId": "bystranger", "title": "B", "artists": [{"name": "Stranger"}]},
        ]
    })
    # limit=8 keeps both unrated candidates under the fluff cap (ceil(8*0.25)=2)
    # -- this test is about the artist boost, not match-quality capping.
    result = recommend.build(yt, db, exclude={"seed", "known"}, feeling="heartbroken", limit=8)
    by_id = {s["videoId"]: s for s in result["songs"]}
    assert by_id["byknown"]["known_artist"] is True
    assert by_id["bystranger"]["known_artist"] is False


def test_build_notes_when_picks_have_no_mood_label(db):
    _library(db, ("seed", "Seeder", "Sad"))
    yt = _BuildYT({"seed": [{"videoId": "unrated", "title": "U", "artists": [{"name": "Nobody"}]}]})
    result = recommend.build(yt, db, exclude={"seed"}, feeling="heartbroken", limit=1)
    assert any("mood label" in n for n in result["notes"])


def test_build_survives_a_seed_whose_signals_all_fail(db):
    _library(db, ("seed", "Seeder", "Sad"))

    class _Broken(_BuildYT):
        def get_watch_playlist(self, videoId, limit=25, radio=True):
            raise YTMusicMCPError("dead")

    result = recommend.build(_Broken({}), db, exclude=set(), feeling="heartbroken", limit=3)
    assert result["songs"] == []


def test_build_pulls_from_mood_playlists_even_with_no_library_seeds(db):
    # The fourth signal: reaches outside the listener's own taste graph.
    store.upsert_tracks(db, [{"videoId": "atlassong", "title": "Atlas", "artists": ["Someone"]}])
    store.put_track_moods(db, "atlas", [("atlassong", ms.ANCHORS["Sad"], 0.9)])
    result = recommend.build(_BuildYT({}), db, exclude=set(), feeling="heartbroken", limit=1)
    assert [s["videoId"] for s in result["songs"]] == ["atlassong"]
    assert result["songs"][0]["sources"] == [recommend.ATLAS_SOURCE]


# --- pick_seeds_from_playlist ----------------------------------------------


def _pl_track(vid, title=None, artist=None):
    return {"videoId": vid, "title": title or vid.upper(), "artists": [{"name": artist or f"Artist {vid}"}]}


def test_playlist_seeds_keep_only_genuine_mood_matches(db):
    tracks = [_pl_track("fits"), _pl_track("misses")]
    store.upsert_tracks(db, tracks)
    store.put_track_moods(db, "atlas", [
        ("fits", ms.ANCHORS["Sad"], 0.9),
        ("misses", ms.ANCHORS["Party"], 0.9),
    ])

    picked = recommend.pick_seeds_from_playlist(db, tracks, ms.ANCHORS["Sad"])

    assert [s["videoId"] for s in picked["seeds"]] == ["fits"]
    assert picked["considered"] == 2
    assert picked["genuine"] == 1


def test_playlist_seeds_skip_tracks_with_no_resolvable_mood(db):
    tracks = [_pl_track("known"), _pl_track("unlabelled")]
    store.upsert_tracks(db, tracks)
    store.put_track_moods(db, "atlas", [("known", ms.ANCHORS["Sad"], 0.9)])

    picked = recommend.pick_seeds_from_playlist(db, tracks, ms.ANCHORS["Sad"])

    assert [s["videoId"] for s in picked["seeds"]] == ["known"]
    assert picked["considered"] == 2


def test_playlist_seeds_are_capped_and_report_the_overflow(db):
    tracks = [_pl_track(f"t{i}", artist=f"Artist {i}") for i in range(30)]
    store.upsert_tracks(db, tracks)
    store.put_track_moods(db, "atlas", [(t["videoId"], ms.ANCHORS["Sad"], 0.9) for t in tracks])

    picked = recommend.pick_seeds_from_playlist(db, tracks, ms.ANCHORS["Sad"], cap=5)

    assert len(picked["seeds"]) == 5
    assert picked["considered"] == 30
    assert picked["genuine"] == 30
    assert picked["capped"] == 25


def test_playlist_seeds_spread_across_artists(db):
    tracks = [_pl_track(f"t{i}", artist="One Artist") for i in range(5)]
    store.upsert_tracks(db, tracks)
    store.put_track_moods(db, "atlas", [(t["videoId"], ms.ANCHORS["Sad"], 0.9) for t in tracks])

    picked = recommend.pick_seeds_from_playlist(db, tracks, ms.ANCHORS["Sad"])

    assert len(picked["seeds"]) == 1


def test_playlist_seeds_empty_when_nothing_fits(db):
    tracks = [_pl_track("party")]
    store.upsert_tracks(db, tracks)
    store.put_track_moods(db, "atlas", [("party", ms.ANCHORS["Party"], 0.9)])

    picked = recommend.pick_seeds_from_playlist(db, tracks, ms.ANCHORS["Sad"])

    assert picked["seeds"] == []
    assert picked["genuine"] == 0


def test_build_accepts_injected_seeds_instead_of_library_seeds(db):
    # No library at all -- pick_seeds would return nothing, so anything that
    # comes back proves the injected seeds were used.
    seeds = [{"videoId": "pl1", "title": "PL1", "artists": "A", "fit": 0.9,
              "seed_score": 0.9, "mood": ms.ANCHORS["Sad"]}]
    yt = _BuildYT({"pl1": [{"videoId": "found", "title": "Found", "artists": [{"name": "Fresh"}]}]})

    result = recommend.build(yt, db, exclude=set(), feeling="heartbroken", limit=5, seeds=seeds)

    assert [s["videoId"] for s in result["songs"]] == ["found"]


def test_build_does_not_re_resolve_a_passed_in_target(db):
    class _NoHistory(_BuildYT):
        def get_history(self):
            raise AssertionError("history must not be read when the target is passed in")

    resolved = recommend.resolve_target(db, None, "heartbroken", None, None)
    seeds = [{"videoId": "pl1", "title": "PL1", "artists": "A", "fit": 0.9,
              "seed_score": 0.9, "mood": ms.ANCHORS["Sad"]}]
    yt = _NoHistory({"pl1": [{"videoId": "found", "title": "F", "artists": [{"name": "X"}]}]})

    result = recommend.build(yt, db, exclude=set(), limit=5, seeds=seeds, resolved=resolved)

    assert result["target"] == {k: round(v, 3) for k, v in resolved["target"].items()}


def test_build_caps_fluff_at_25_percent_of_requested(db):
    _library(db, ("seed", "Seeder", "Sad"))
    genuine = [
        {"videoId": f"genuine{i}", "title": f"Genuine {i}", "artists": [{"name": f"GenuineArtist{i}"}]}
        for i in range(7)
    ]
    filler = [
        {"videoId": f"filler{i}", "title": f"Filler {i}", "artists": [{"name": f"FillerArtist{i}"}]}
        for i in range(40)
    ]
    yt = _BuildYT({"seed": genuine + filler})
    store.put_track_moods(db, "atlas", [(t["videoId"], ms.ANCHORS["Sad"], 0.9) for t in genuine])

    result = recommend.build(yt, db, exclude={"seed"}, feeling="heartbroken", arc="mirror", limit=100)

    assert result["match_quality"] == {"genuine": 7, "requested": 100, "fluff_cap": 25, "fluff_used": 25}
    assert len(result["songs"]) == 32
    assert all(vid.startswith("genuine") for vid in [s["videoId"] for s in result["songs"][:7]])
    assert any("Only 7 songs genuinely matched" in n for n in result["notes"])


def test_build_never_caps_when_enough_genuine_matches_exist(db):
    _library(db, ("seed", "Seeder", "Sad"))
    genuine = [
        {"videoId": f"genuine{i}", "title": f"Genuine {i}", "artists": [{"name": f"GenuineArtist{i}"}]}
        for i in range(5)
    ]
    yt = _BuildYT({"seed": genuine})
    store.put_track_moods(db, "atlas", [(t["videoId"], ms.ANCHORS["Sad"], 0.9) for t in genuine])

    result = recommend.build(yt, db, exclude={"seed"}, feeling="heartbroken", limit=5)

    assert len(result["songs"]) == 5
    assert result["match_quality"]["genuine"] == 5
    assert not any("genuinely matched" in n for n in result["notes"])


def test_build_collapses_remix_variants(db):
    _library(db, ("seed", "Seeder", "Sad"))
    yt = _BuildYT({
        "seed": [
            {"videoId": "plain", "title": "Dead and Gone", "artists": [{"name": "T.I."}]},
            {
                "videoId": "remix",
                "title": "Dead and Gone (feat. Justin Timberlake)",
                "artists": [{"name": "T.I."}, {"name": "Justin Timberlake"}],
            },
        ]
    })
    result = recommend.build(yt, db, exclude={"seed"}, feeling="heartbroken", limit=5)
    assert len(result["songs"]) == 1
    assert any("remix/feature variant" in n for n in result["notes"])


def test_build_never_recommends_its_own_seed(db):
    # The seed is in the atlas too, so it can come back around as a neighbour.
    _library(db, ("seed", "Seeder", "Sad"))
    result = recommend.build(_BuildYT({}), db, exclude=set(), feeling="heartbroken", limit=5)
    assert "seed" not in [s["videoId"] for s in result["songs"]]


# --- label.sync_library ----------------------------------------------------


class _LibraryYT:
    def __init__(self, liked, playlists, listing, broken=()):
        self._liked, self._playlists = liked, playlists
        self._listing, self._broken = listing, set(broken)

    def get_playlist(self, playlist_id, limit=None):
        if playlist_id in self._broken:
            raise YTMusicError("gone")
        if playlist_id == "LM":
            return {"tracks": self._liked}
        return {"tracks": self._playlists[playlist_id]}

    def get_library_playlists(self, limit=None):
        return self._listing


def test_sync_library_records_liked_and_playlist_membership(db):
    yt = _LibraryYT(
        liked=[{"videoId": "a", "title": "A", "artists": [{"name": "X"}]}],
        playlists={"PL1": [{"videoId": "b", "title": "B", "artists": [{"name": "Y"}]}]},
        listing=[{"playlistId": "PL1", "title": "C - Punjabi"}],
    )
    result = label.sync_library(db, yt)
    assert result == {"playlists": 1, "rows": 2, "unique_tracks": 2}
    assert label.genre_prior(db, "b") == "Punjabi"
    assert store.get_track(db, "a")["artists"] == "X"


def test_sync_library_skips_the_lm_entry_in_the_listing(db):
    yt = _LibraryYT(liked=[{"videoId": "a"}], playlists={}, listing=[{"playlistId": "LM", "title": "Liked Music"}])
    assert label.sync_library(db, yt)["playlists"] == 0


def test_sync_library_skips_a_playlist_that_fails_to_fetch(db):
    yt = _LibraryYT(
        liked=[{"videoId": "a"}],
        playlists={"PL2": [{"videoId": "c"}]},
        listing=[{"playlistId": "PL1", "title": "Broken"}, {"playlistId": "PL2", "title": "Fine"}],
        broken={"PL1"},
    )
    assert label.sync_library(db, yt)["unique_tracks"] == 2


def test_sync_library_ignores_playlists_with_no_title(db):
    yt = _LibraryYT(liked=[], playlists={}, listing=[{"playlistId": "PL1"}])
    assert label.sync_library(db, yt)["playlists"] == 0


# --- v2 tool surface -------------------------------------------------------


@pytest.fixture
def wired(db, monkeypatch):
    """Point the server's client and store at fakes."""
    import server

    yt = _BuildYT({"seed": [{"videoId": "new1", "title": "New One", "artists": [{"name": "Fresh"}]}]})
    monkeypatch.setattr(server, "_client", lambda: yt)
    monkeypatch.setattr(server, "_store", lambda: db)
    monkeypatch.setattr(server, "_library_video_ids", lambda _yt: {"seed"})
    return server, db, yt


def test_recommend_for_mood_returns_a_sequence_and_logs_it(wired):
    server, db, _ = wired
    _library(db, ("seed", "Seeder", "Sad"))

    result = server.recommend_for_mood(feeling="heartbroken", arc="mirror", limit=1)
    assert [s["videoId"] for s in result["songs"]] == ["new1"]
    logged = db.execute("SELECT video_id, feeling, arc FROM recommendation").fetchall()
    assert [tuple(r) for r in logged] == [("new1", "heartbroken", "mirror")]


class _PlaylistYT(_BuildYT):
    """_BuildYT plus a readable playlist, for the mood+playlist tool."""

    def __init__(self, radio, playlist_tracks):
        super().__init__(radio)
        self._playlist_tracks = playlist_tracks

    def get_playlist(self, playlistId, limit=None):
        return {"tracks": self._playlist_tracks}


@pytest.fixture
def wired_playlist(db, monkeypatch):
    import server

    monkeypatch.setattr(server, "_store", lambda: db)
    monkeypatch.setattr(server, "_library_video_ids", lambda _yt: set())

    def _wire(radio, playlist_tracks):
        yt = _PlaylistYT(radio, playlist_tracks)
        monkeypatch.setattr(server, "_client", lambda: yt)
        return server, db, yt

    return _wire


def test_playlist_mood_tool_seeds_only_from_fitting_tracks(wired_playlist):
    fits, misses = _pl_track("fits"), _pl_track("misses")
    server, db, _ = wired_playlist(
        {"fits": [{"videoId": "found", "title": "Found", "artists": [{"name": "Fresh"}]}],
         "misses": [{"videoId": "wrong", "title": "Wrong", "artists": [{"name": "Off"}]}]},
        [fits, misses],
    )
    store.upsert_tracks(db, [fits, misses])
    store.put_track_moods(db, "atlas", [
        ("fits", ms.ANCHORS["Sad"], 0.9),
        ("misses", ms.ANCHORS["Party"], 0.9),
    ])

    result = server.recommend_from_playlist_for_mood("PL1", feeling="heartbroken", limit=5)

    assert result["seed_report"]["considered"] == 2
    assert result["seed_report"]["genuine"] == 1
    assert result["seed_report"]["seeded_from"] == 1
    # "wrong" came from the off-mood track, which never got to seed.
    assert "wrong" not in [s["videoId"] for s in result["songs"]]
    assert "found" in [s["videoId"] for s in result["songs"]]


def test_playlist_mood_tool_never_returns_the_playlists_own_tracks(wired_playlist):
    fits = _pl_track("fits")
    server, db, _ = wired_playlist(
        {"fits": [{"videoId": "fits", "title": "FITS", "artists": [{"name": "Artist fits"}]}]},
        [fits],
    )
    store.upsert_tracks(db, [fits])
    store.put_track_moods(db, "atlas", [("fits", ms.ANCHORS["Sad"], 0.9)])

    result = server.recommend_from_playlist_for_mood("PL1", feeling="heartbroken", limit=5)

    assert "fits" not in [s["videoId"] for s in result["songs"]]


def test_playlist_mood_tool_explains_itself_when_nothing_fits(wired_playlist):
    party = _pl_track("party")
    server, db, _ = wired_playlist({}, [party])
    store.upsert_tracks(db, [party])
    store.put_track_moods(db, "atlas", [("party", ms.ANCHORS["Party"], 0.9)])

    with pytest.raises(RuntimeError, match="fits that mood"):
        server.recommend_from_playlist_for_mood("PL1", feeling="heartbroken", limit=5)


def test_playlist_mood_tool_rejects_an_empty_playlist(wired_playlist):
    server, _, _ = wired_playlist({}, [])
    with pytest.raises(RuntimeError, match="no playable tracks"):
        server.recommend_from_playlist_for_mood("PL1", feeling="heartbroken")


def test_recommend_for_mood_honours_recorded_rejections(wired):
    server, db, _ = wired
    _library(db, ("seed", "Seeder", "Sad"))
    store.put_feedback(db, "new1", "wrong_mood")
    assert server.recommend_for_mood(feeling="heartbroken", limit=1)["songs"] == []


def test_recommend_for_mood_accepts_an_explicit_vector(wired):
    server, db, _ = wired
    _library(db, ("seed", "Seeder", "Sad"))
    result = server.recommend_for_mood(vector=ms.vector(valence=-0.9, energy=0.2), limit=1)
    assert result["target_origin"] == "explicit"
    assert result["target"]["valence"] == pytest.approx(-0.9)


def test_recommend_for_mood_rejects_an_unknown_arc(wired):
    server, db, _ = wired
    _library(db, ("seed", "Seeder", "Sad"))
    with pytest.raises(RuntimeError, match="Unknown arc"):
        server.recommend_for_mood(feeling="heartbroken", arc="sideways")


def test_read_my_mood_reports_evidence_not_just_a_verdict(wired):
    server, db, _ = wired
    _seed_history(db, [("a", "Joyner Lucas", "Sad", sense.REPEAT_THRESHOLD)])
    result = server.read_my_mood()
    assert result["described"] is not None
    assert any("On repeat" in e for e in result["evidence"])


def test_read_my_mood_on_an_empty_history(wired):
    server, _, _ = wired
    assert server.read_my_mood()["described"] is None


def test_explain_recommendation_reports_source_and_context(wired):
    server, db, _ = wired
    _library(db, ("seed", "Seeder", "Sad"))
    server.recommend_for_mood(feeling="heartbroken", limit=1)

    explained = server.explain_recommendation("new1")
    assert explained["title"] == "New One"
    assert explained["last_served_against"]["feeling"] == "heartbroken"


def test_explain_recommendation_on_an_unknown_song(wired):
    server, _, _ = wired
    explained = server.explain_recommendation("nope")
    assert explained["mood"] is None and explained["last_served_against"] is None


def test_record_feedback_validates_the_reaction(wired):
    server, _, _ = wired
    with pytest.raises(RuntimeError, match="reaction must be one of"):
        server.record_feedback("a", "meh")


def test_record_feedback_stores_a_valid_reaction(wired):
    server, db, _ = wired
    assert server.record_feedback("a", "loved")["recorded"] is True
    assert store.feedback_for(db, "a")[0]["reaction"] == "loved"


def test_index_status_reports_coverage_and_llm_availability(wired):
    server, db, _ = wired
    _library(db, ("seed", "Seeder", "Sad"))
    status = server.index_status()
    assert status["library"]["coverage"] == 1.0
    assert "available" in status["llm_labelling"]


def test_build_records_the_identity_of_what_it_served(db):
    # explain_recommendation has to be able to name the song afterwards.
    _library(db, ("seed", "Seeder", "Sad"))
    yt = _BuildYT({"seed": [{"videoId": "new1", "title": "New One", "artists": [{"name": "Fresh"}]}]})
    recommend.build(yt, db, exclude={"seed"}, feeling="heartbroken", limit=1)
    assert store.get_track(db, "new1")["title"] == "New One"


def test_hold_arc_peaks_at_the_target_not_above_it(db):
    # Workout sits at 0.95 energy. Adding a hump would clamp to 1.0 and flatten
    # the curve for exactly the moods this arc exists for.
    start = ms.ANCHORS["Workout"]
    energies = [t["energy"] for t in arc.targets(start, "hold", 5)]
    assert max(energies) == pytest.approx(start["energy"])
    assert energies[0] < energies[2]
    assert len(set(energies)) > 1  # a real curve, not a clamped flat line


def test_one_playlist_listed_under_two_moods_records_both(db):
    # YouTube lists the same playlist under several moods. Keying the resume
    # point on playlist_id alone dropped 874 of 1,979 listings on a real crawl.
    store.record_playlist(db, "PL1", "Chill", "Mixed", [{"videoId": "a"}])
    store.record_playlist(db, "PL1", "Commute", "Mixed", [{"videoId": "a"}])
    assert store.atlas_mood_counts(db, "a") == {"Chill": 1, "Commute": 1}
    assert store.crawled_playlist_moods(db) == {("PL1", "Chill"), ("PL1", "Commute")}


def test_crawl_revisits_a_playlist_under_a_different_mood(db):
    yt = _FakeYT(
        categories={"Moods & moments": [
            {"title": "Chill", "params": "p-chill"},
            {"title": "Commute", "params": "p-commute"},
        ]},
        mood_playlists={
            "p-chill": [{"playlistId": "PL1", "title": "Mixed"}],
            "p-commute": [{"playlistId": "PL1", "title": "Mixed"}],
        },
        playlists={"PL1": {"tracks": [{"videoId": "a"}]}},
    )
    stats = atlas.crawl(yt, db, moods=["Chill", "Commute"], sleep=_noop_sleep)
    assert stats["ok"] == 2
    assert sorted(store.atlas_mood_counts(db, "a")) == ["Chill", "Commute"]


# --- distinctiveness scoring -----------------------------------------------


def _central_vector():
    """A vector at the middle of the space -- fits everything, is nothing."""
    return ms.blend([(anchor, 1.0) for anchor in ms.ANCHORS.values()])


def test_a_central_vector_has_a_narrower_spread_than_a_distinctive_one():
    # The anchors aren't uniformly distributed (Sad is an outlier), so a central
    # vector isn't equidistant from all of them. What matters is that it never
    # stands out strongly for any single mood the way a distinctive song does.
    def spread(vec):
        scores = [ms.relative_fit(vec, anchor) for anchor in ms.ANCHORS.values()]
        return max(scores) - min(scores)

    assert spread(_central_vector()) < spread(ms.ANCHORS["Sad"])


def test_a_central_vector_is_never_strongly_its_own_mood():
    central = _central_vector()
    best = max(ms.relative_fit(central, anchor) for anchor in ms.ANCHORS.values())
    assert best < ms.relative_fit(ms.ANCHORS["Sad"], ms.ANCHORS["Sad"])


def test_relative_fit_is_positive_for_its_own_mood_and_negative_elsewhere():
    sad = ms.ANCHORS["Sad"]
    assert ms.relative_fit(sad, sad) > 0.2
    assert ms.relative_fit(sad, ms.ANCHORS["Party"]) < 0


def test_seed_score_prefers_a_distinctive_song_over_a_bland_one():
    # The bug this exists for: raw fit made one bland song the top seed for
    # heartbroken, angry, Party and Focus alike.
    sad, central = ms.ANCHORS["Sad"], _central_vector()
    assert ms.seed_score(sad, sad, 0.6) > ms.seed_score(central, sad, 1.0)


def test_a_central_song_does_not_win_every_mood():
    central = _central_vector()
    for target in (ms.ANCHORS["Sad"], ms.ANCHORS["Party"], ms.ANCHORS["Focus"], ms.ANCHORS["Workout"]):
        assert ms.seed_score(target, target, 1.0) > ms.seed_score(central, target, 1.0)


def test_confidence_shifts_seed_score_without_dominating_it():
    sad, central = ms.ANCHORS["Sad"], _central_vector()
    assert ms.seed_score(sad, sad, 1.0) > ms.seed_score(sad, sad, 0.0)
    # ...but even at zero confidence, distinctive still beats confident-and-bland.
    assert ms.seed_score(sad, sad, 0.0) > ms.seed_score(central, sad, 1.0)


def test_seed_score_clamps_out_of_range_confidence():
    sad = ms.ANCHORS["Sad"]
    assert ms.seed_score(sad, sad, 5.0) == pytest.approx(ms.seed_score(sad, sad, 1.0))
    assert ms.seed_score(sad, sad, -5.0) == pytest.approx(ms.seed_score(sad, sad, 0.0))


def test_pick_seeds_ranks_by_distinctiveness_not_raw_fit(db):
    store.sync_library(db, [("bland", "Liked Music", True), ("distinct", "Liked Music", True)])
    store.upsert_tracks(db, [
        {"videoId": "bland", "title": "Bland", "artists": ["A"]},
        {"videoId": "distinct", "title": "Distinct", "artists": ["B"]},
    ])
    store.put_track_moods(db, "atlas", [
        ("bland", _central_vector(), 1.0),          # maximum confidence, no character
        ("distinct", ms.ANCHORS["Sad"], 0.5),       # half the confidence, unmistakably sad
    ])
    seeds = recommend.pick_seeds(db, ms.ANCHORS["Sad"], count=2)
    assert seeds[0]["videoId"] == "distinct"


# --- seed re-upload exclusion ----------------------------------------------


def test_same_song_matches_re_uploads():
    import signals

    assert signals.same_song("Kryptonite", "3 Doors Down", "Kryptonite", "3 Doors Down")
    assert signals.same_song("Kryptonite", "3 Doors Down", "Kryptonite (Official Video)", "3 Doors Down")
    assert signals.same_song("Kryptonite", "3 Doors Down", "Kryptonite [Remastered]", "3 Doors Down Topic")
    assert signals.same_song("Bulleya", None, "Bulleya", "Amit Mishra")


def test_same_song_does_not_over_match():
    import signals

    assert not signals.same_song("Kryptonite", "3 Doors Down", "Kryptonite", "Big Boi")
    assert not signals.same_song("Kryptonite", "3 Doors Down", "Here Without You", "3 Doors Down")
    assert not signals.same_song(None, "x", "Kryptonite", "x")


# --- bridge expansion across a language filter -----------------------------


def _bridge_db(db):
    """Two English songs and one Punjabi one, all recognisable by genre."""
    store.upsert_tracks(db, [
        {"videoId": "eng1", "title": "Eng One", "artists": ["Kendrick Lamar"]},
        {"videoId": "eng2", "title": "Eng Two", "artists": ["Drake"]},
        {"videoId": "pun1", "title": "Pun One", "artists": ["Karan Aujla"]},
    ])
    store.record_genre(db, "Hip-hop", ["eng1", "eng2"])
    store.record_genre(db, "Bollywood & Indian", ["pun1"])


def test_bridge_expand_is_skipped_when_there_is_already_enough(db):
    survivors = [{"videoId": "eng1", "title": "Eng One", "artists": ["Kendrick Lamar"], "base_score": 2}]
    out, added = recommend.bridge_expand(
        _BuildYT({}), db, survivors, exclude=set(), want=["english"],
        exclude_languages=None, allow_unlabelled=False, needed=1,
    )
    assert out == survivors and added == 0


def test_bridge_expand_reseeds_from_the_songs_that_passed(db):
    # Filtering a Punjabi-seeded pool for English leaves almost nothing, so the
    # survivors become seeds to reach more of that language.
    _bridge_db(db)
    yt = _BuildYT({"eng1": [{"videoId": "eng2", "title": "Eng Two", "artists": [{"name": "Drake"}]}]})
    survivors = [{"videoId": "eng1", "title": "Eng One", "artists": ["Kendrick Lamar"], "base_score": 2}]

    out, added = recommend.bridge_expand(
        yt, db, survivors, exclude=set(), want=["english"],
        exclude_languages=None, allow_unlabelled=False, needed=4,
    )
    assert added == 1
    assert sorted(c["videoId"] for c in out) == ["eng1", "eng2"]


def test_bridge_expand_still_honours_the_language_filter(db):
    _bridge_db(db)
    yt = _BuildYT({"eng1": [{"videoId": "pun1", "title": "Pun One", "artists": [{"name": "Karan Aujla"}]}]})
    survivors = [{"videoId": "eng1", "title": "Eng One", "artists": ["Kendrick Lamar"], "base_score": 2}]

    out, added = recommend.bridge_expand(
        yt, db, survivors, exclude=set(), want=["english"],
        exclude_languages=None, allow_unlabelled=False, needed=4,
    )
    assert added == 0
    assert [c["videoId"] for c in out] == ["eng1"]


def test_bridge_expand_respects_the_exclusion_set(db):
    _bridge_db(db)
    yt = _BuildYT({"eng1": [{"videoId": "eng2", "title": "Eng Two", "artists": [{"name": "Drake"}]}]})
    survivors = [{"videoId": "eng1", "title": "Eng One", "artists": ["Kendrick Lamar"], "base_score": 2}]

    out, added = recommend.bridge_expand(
        yt, db, survivors, exclude={"eng2"}, want=["english"],
        exclude_languages=None, allow_unlabelled=False, needed=4,
    )
    assert added == 0


def test_bridge_expand_gives_up_quietly_with_nothing_to_bridge_from(db):
    out, added = recommend.bridge_expand(
        _BuildYT({}), db, [], exclude=set(), want=["english"],
        exclude_languages=None, allow_unlabelled=False, needed=5,
    )
    assert out == [] and added == 0


def test_bridge_expand_survives_a_dead_bridge(db):
    _bridge_db(db)

    class _Dead(_BuildYT):
        def get_watch_playlist(self, videoId, limit=25, radio=True):
            raise YTMusicMCPError("gone")

    survivors = [{"videoId": "eng1", "title": "Eng One", "artists": ["Kendrick"], "base_score": 2}]
    out, added = recommend.bridge_expand(
        _Dead({}), db, survivors, exclude=set(), want=["english"],
        exclude_languages=None, allow_unlabelled=False, needed=5,
    )
    assert added == 0 and len(out) == 1


# --- the notes users actually read -----------------------------------------


def test_language_note_reports_both_kinds_of_drop():
    note = recommend._language_note(
        {"applied": True, "want": ["english"], "exclude": None, "kept": 3,
         "dropped_wrong_language": 26, "dropped_unlabelled": 28, "allow_unlabelled": False},
        limit=8,
    )
    assert "26 in other languages" in note
    assert "28 with no language label" in note
    assert "allow_unlabelled_language=True" in note
    assert "fewer than the 8 requested" in note


def test_language_note_stays_quiet_when_nothing_was_lost():
    note = recommend._language_note(
        {"applied": True, "want": ["english"], "exclude": None, "kept": 10,
         "dropped_wrong_language": 0, "dropped_unlabelled": 0, "allow_unlabelled": False},
        limit=10,
    )
    assert "dropped" not in note and "fewer than" not in note


def test_language_note_describes_an_exclusion_filter():
    note = recommend._language_note(
        {"applied": True, "want": None, "exclude": ["punjabi"], "kept": 10,
         "dropped_wrong_language": 2, "dropped_unlabelled": 0, "allow_unlabelled": True},
        limit=10,
    )
    assert "not punjabi" in note


def test_tempo_note_explains_why_unknown_bpm_is_kept():
    note = recommend._tempo_note(
        {"applied": True, "target_bpm": 99.0, "range": None, "with_known_tempo": 28,
         "unknown_tempo_kept": 44, "dropped_out_of_range": 0}
    )
    assert "99bpm" in note and "44 kept with unknown BPM" in note


def test_tempo_note_reports_a_hard_range():
    note = recommend._tempo_note(
        {"applied": True, "target_bpm": None, "range": [90, 110], "with_known_tempo": 31,
         "unknown_tempo_kept": 0, "dropped_out_of_range": 25}
    )
    assert "90-110bpm" in note and "25 dropped as out of range" in note
