"""Implicit feedback: learning from what was recommended vs. what got played.

The explicit feedback path (record_feedback) is covered in test_v2.py. This
covers the inference that runs without anyone being asked -- the diff between
the `recommendation` and `history_log` tables -- and the bounded artist
affinity it feeds.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import moodspace as ms  # noqa: E402
import recommend  # noqa: E402
import store  # noqa: E402


def _served(db, video_id, at):
    """Log one recommendation of `video_id`, served at `at`."""
    target = ms.ANCHORS["Sad"]
    db.execute(
        "INSERT INTO recommendation (video_id, served_at, feeling, arc, slot, score, "
        "  valence, energy, tension, depth) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (video_id, at, "sad", "mirror", 0, 1.0,
         target["valence"], target["energy"], target["tension"], target["depth"]),
    )
    db.commit()


def _snapshots(db, stamps, video_ids=()):
    """Take history snapshots at `stamps`, each containing `video_ids`."""
    for stamp in stamps:
        store.log_history(db, [{"videoId": v} for v in video_ids] or [{"videoId": "unrelated"}],
                          observed_at=stamp)


# --- infer_implicit_feedback ------------------------------------------------


def test_a_recommended_song_played_afterwards_counts_as_played(db):
    _served(db, "a", 100.0)
    _snapshots(db, [200.0], ["a"])

    result = store.infer_implicit_feedback(db)
    assert result["played"] == 1
    assert [f["reaction"] for f in store.feedback_for(db, "a")] == ["played"]


def test_a_play_before_the_recommendation_is_not_evidence(db):
    # They were already listening to it; the recommendation didn't cause that.
    _served(db, "a", 500.0)
    _snapshots(db, [100.0], ["a"])
    _snapshots(db, [600.0, 700.0, 800.0])

    result = store.infer_implicit_feedback(db)
    assert result["played"] == 0
    assert result["ignored"] == 1


def test_absence_only_counts_once_enough_snapshots_have_been_taken(db):
    _served(db, "a", 100.0)
    _snapshots(db, [200.0, 300.0])  # two snapshots, below the threshold of three

    result = store.infer_implicit_feedback(db)
    assert result == {"considered": 1, "played": 0, "ignored": 0, "retracted": 0, "pending": 1}
    assert store.feedback_for(db, "a") == []


def test_absence_across_enough_snapshots_counts_as_ignored(db):
    _served(db, "a", 100.0)
    _snapshots(db, [200.0, 300.0, 400.0])

    result = store.infer_implicit_feedback(db)
    assert result["ignored"] == 1
    assert [f["reaction"] for f in store.feedback_for(db, "a")] == ["ignored"]


def test_inference_is_idempotent(db):
    _served(db, "a", 100.0)
    _snapshots(db, [200.0], ["a"])

    store.infer_implicit_feedback(db)
    second = store.infer_implicit_feedback(db)
    assert second["played"] == 0
    assert len(store.feedback_for(db, "a")) == 1


def test_a_later_play_retracts_an_earlier_ignored_verdict(db):
    _served(db, "a", 100.0)
    _snapshots(db, [200.0, 300.0, 400.0])
    store.infer_implicit_feedback(db)
    assert [f["reaction"] for f in store.feedback_for(db, "a")] == ["ignored"]

    _snapshots(db, [500.0], ["a"])
    result = store.infer_implicit_feedback(db)

    assert result["retracted"] == 1
    assert [f["reaction"] for f in store.feedback_for(db, "a")] == ["played"]


def test_nothing_recommended_yet_infers_nothing(db):
    assert store.infer_implicit_feedback(db)["considered"] == 0


# --- implicit feedback must never hard-exclude ------------------------------


def test_an_ignored_song_is_not_permanently_rejected(db):
    """The whole point of a separate reaction name: inferred absence demotes,
    it never triggers the permanent exclusion that a stated skip does."""
    _served(db, "a", 100.0)
    _snapshots(db, [200.0, 300.0, 400.0])
    store.infer_implicit_feedback(db)

    assert store.feedback_for(db, "a")[0]["reaction"] == "ignored"
    assert store.rejected_video_ids(db) == set()


def test_an_explicit_skip_still_rejects(db):
    store.put_feedback(db, "a", "skipped")
    assert store.rejected_video_ids(db) == {"a"}


def test_an_implicitly_sourced_skip_would_still_not_reject(db):
    # Defensive: if a future inference ever writes 'skipped', the source guard
    # keeps it out of the hard exclusion set rather than silently blocking.
    store.put_feedback(db, "a", "skipped", source=store.IMPLICIT_SOURCE)
    assert store.rejected_video_ids(db) == set()


# --- feedback_counts / feedback_stats ---------------------------------------


def test_counts_pool_explicit_and_implicit_reactions(db):
    store.put_feedback(db, "a", "loved")
    store.put_feedback(db, "a", "played", source=store.IMPLICIT_SOURCE)
    store.put_feedback(db, "b", "ignored", source=store.IMPLICIT_SOURCE)

    counts = store.feedback_counts(db)
    assert counts["a"] == {"positive": 2, "negative": 0}
    assert counts["b"] == {"positive": 0, "negative": 1}


def test_stats_separate_what_was_said_from_what_was_inferred(db):
    _served(db, "a", 100.0)
    store.put_feedback(db, "a", "loved")
    store.put_feedback(db, "b", "ignored", source=store.IMPLICIT_SOURCE)
    _snapshots(db, [200.0])

    stats = store.feedback_stats(db)
    assert stats["recommended_songs"] == 1
    assert stats["history_snapshots"] == 1
    assert stats["explicit"] == {"loved": 1}
    assert stats["implicit"] == {"ignored": 1}


# --- artist_affinity --------------------------------------------------------


def _track(db, video_id, artist):
    store.upsert_tracks(db, [{"videoId": video_id, "title": video_id.upper(), "artists": [artist]}])


def test_no_feedback_means_no_affinity(db):
    assert recommend.artist_affinity(db) == {}


def test_positive_feedback_boosts_the_artist(db):
    _track(db, "a", "Loved One")
    store.put_feedback(db, "a", "loved")

    affinity = recommend.artist_affinity(db)
    assert 1.0 < affinity["loved one"] <= recommend.AFFINITY_MAX_BOOST


def test_negative_feedback_demotes_the_artist(db):
    _track(db, "a", "Ignored One")
    store.put_feedback(db, "a", "ignored", source=store.IMPLICIT_SOURCE)

    affinity = recommend.artist_affinity(db)
    assert recommend.AFFINITY_MAX_PENALTY <= affinity["ignored one"] < 1.0


def test_feedback_aggregates_across_an_artists_songs(db):
    _track(db, "a", "Same Artist")
    _track(db, "b", "Same Artist")
    store.put_feedback(db, "a", "loved")
    store.put_feedback(db, "b", "saved")

    # Two positives on one artist beat one, up to the saturation point.
    two = recommend.artist_affinity(db)["same artist"]
    store.put_feedback(db, "b", "played", source=store.IMPLICIT_SOURCE)
    three = recommend.artist_affinity(db)["same artist"]
    assert three > two


def test_affinity_saturates_rather_than_running_away(db):
    _track(db, "a", "Adored")
    for _ in range(50):
        store.put_feedback(db, "a", "loved")

    assert recommend.artist_affinity(db)["adored"] == pytest.approx(recommend.AFFINITY_MAX_BOOST)


def test_opposing_feedback_cancels_out(db):
    _track(db, "a", "Mixed")
    store.put_feedback(db, "a", "loved")
    store.put_feedback(db, "a", "skipped")

    # Net zero is omitted entirely rather than returned as 1.0, so the
    # ranking loop stays on its unchanged path.
    assert "mixed" not in recommend.artist_affinity(db)


def test_feedback_on_an_unknown_track_is_ignored(db):
    # Nothing to attribute it to -- no track row means no artist.
    store.put_feedback(db, "ghost", "loved")
    assert recommend.artist_affinity(db) == {}


# --- end to end through recommend.build -------------------------------------


class _BuildYT:
    def __init__(self, radio):
        self._radio = radio

    def get_watch_playlist(self, videoId, limit=25, radio=True):
        return {"tracks": self._radio.get(videoId, [])}

    def get_history(self):
        return []


def _library(db, *songs):
    store.sync_library(db, [(vid, "Liked Music", True) for vid, _, _ in songs])
    store.upsert_tracks(db, [{"videoId": v, "title": v.upper(), "artists": [a]} for v, a, _ in songs])
    store.put_track_moods(db, "atlas", [(v, ms.ANCHORS[anchor], 0.9) for v, _, anchor in songs])


def test_build_ranks_a_liked_artist_above_an_ignored_one(db):
    _library(db, ("seed", "Seeder", "Sad"))
    yt = _BuildYT({
        "seed": [
            {"videoId": "fromignored", "title": "A", "artists": [{"name": "Ignored One"}]},
            {"videoId": "fromloved", "title": "B", "artists": [{"name": "Loved One"}]},
        ]
    })
    # Both candidates carry identical signal scores, so affinity is the only
    # thing that can separate them.
    _track(db, "past_a", "Ignored One")
    _track(db, "past_b", "Loved One")
    store.put_feedback(db, "past_a", "ignored", source=store.IMPLICIT_SOURCE)
    store.put_feedback(db, "past_b", "played", source=store.IMPLICIT_SOURCE)

    result = recommend.build(yt, db, exclude={"seed"}, feeling="heartbroken", limit=8)
    order = [s["videoId"] for s in result["songs"]]
    assert order.index("fromloved") < order.index("fromignored")


def test_build_reports_the_affinity_it_applied(db):
    _library(db, ("seed", "Seeder", "Sad"))
    yt = _BuildYT({"seed": [{"videoId": "new1", "title": "A", "artists": [{"name": "Loved One"}]}]})
    _track(db, "past", "Loved One")
    store.put_feedback(db, "past", "loved")

    result = recommend.build(yt, db, exclude={"seed"}, feeling="heartbroken", limit=1)
    assert result["songs"][0]["affinity"] > 1.0


def test_build_omits_affinity_when_nothing_was_learned(db):
    _library(db, ("seed", "Seeder", "Sad"))
    yt = _BuildYT({"seed": [{"videoId": "new1", "title": "A", "artists": [{"name": "Nobody"}]}]})

    result = recommend.build(yt, db, exclude={"seed"}, feeling="heartbroken", limit=1)
    assert "affinity" not in result["songs"][0]
