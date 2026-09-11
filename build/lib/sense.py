"""Reading the listener's current mood from what they've actually been playing.

What someone types is the strongest signal and always wins. But people often
don't say -- they say "put something on" -- and their recent listening says
plenty. A song on repeat is the most reliable emotional signal available
anywhere in this system.

One real limitation, worth stating rather than papering over: get_history()
reports only "Today" or "Yesterday". There are no timestamps, so this gives
*order*, never *hour*. snapshot() exists to fix that over time -- stamping each
observation with a real local clock is the only way this system will ever know
that someone was listening at 2am.
"""

import time
from typing import Any

import label
import moodspace
import store

# How many recent plays to read. Far enough back to see a mood, near enough
# that last week's commute doesn't outvote this evening.
WINDOW = 25

# Recency weighting: the most recent play counts fully, the oldest in the
# window about a third as much.
_MIN_RECENCY_WEIGHT = 0.35

REPEAT_THRESHOLD = 3


def snapshot(conn: Any, yt: Any) -> int:
    """Record the current history with a real local timestamp."""
    return store.log_history(conn, yt.get_history())


def _recency_weights(count: int) -> list[float]:
    if count <= 1:
        return [1.0] * count
    step = (1.0 - _MIN_RECENCY_WEIGHT) / (count - 1)
    return [1.0 - step * i for i in range(count)]


def read_mood(conn: Any, yt: Any | None = None, window: int = WINDOW) -> dict[str, Any]:
    """Infer the listener's current mood, with the evidence for it.

    The evidence matters as much as the verdict. A recommender that says "you
    seem low" is guessing at someone; one that says "you've had these three on
    loop since yesterday" is showing its work and letting them correct it.
    """
    if yt is not None:
        try:
            snapshot(conn, yt)
        except Exception:  # noqa: BLE001 - a stale read beats no read
            pass

    recent = store.recent_history(conn, limit=window)
    if not recent:
        return {"vector": None, "confidence": 0.0, "evidence": ["No listening history recorded yet."]}

    video_ids = [row["video_id"] for row in recent]
    moods = label.resolve_or_derive(conn, video_ids)
    weights = _recency_weights(len(video_ids))

    weighted = []
    for video_id, recency in zip(video_ids, weights):
        entry = moods.get(video_id)
        if entry:
            weighted.append((entry["vector"], recency * max(entry["confidence"], 0.1)))

    evidence: list[str] = []
    tracks = {r["video_id"]: store.get_track(conn, r["video_id"]) or {} for r in recent}

    repeats = [r for r in recent if (r["snapshots"] or 0) >= REPEAT_THRESHOLD]
    if repeats:
        titles = [tracks[r["video_id"]].get("title") or "?" for r in repeats[:3]]
        evidence.append(f"On repeat: {', '.join(titles)} — the strongest signal here.")

    artists = [label.primary_artist(tracks[vid].get("artists")) for vid in video_ids[:12]]
    artists = [a for a in artists if a]
    if artists:
        top = max(set(artists), key=artists.count)
        share = artists.count(top) / len(artists)
        if share >= 0.4:
            evidence.append(f"{artists.count(top)} of the last {len(artists)} plays are {top.title()}.")

    if not weighted:
        return {
            "vector": None,
            "confidence": 0.0,
            "evidence": evidence + [
                f"{len(recent)} recent plays, but none of them have a mood label yet — "
                "run scripts/label_library.py to fix that."
            ],
            "coverage": 0.0,
        }

    vector = moodspace.blend(weighted)
    coverage = len(weighted) / len(video_ids)

    half = max(1, len(weighted) // 2)
    older = moodspace.blend(weighted[half:])
    newer = moodspace.blend(weighted[:half])
    if older and newer:
        drift = newer["valence"] - older["valence"]
        if abs(drift) > 0.25:
            direction = "lifting" if drift > 0 else "sinking"
            evidence.append(f"Mood is {direction} across the session (valence {drift:+.2f}).")

    evidence.append(f"Read from {len(weighted)} of {len(video_ids)} recent plays: {moodspace.describe(vector)}.")

    return {
        "vector": vector,
        "confidence": round(min(1.0, coverage * (len(weighted) / 10)), 3),
        "coverage": round(coverage, 3),
        "evidence": evidence,
        "sampled": len(video_ids),
    }


def time_of_day_prior() -> dict[str, Any]:
    """A weak contextual nudge. Never overrides what someone actually said."""
    hour = time.localtime().tm_hour
    if hour < 6:
        return {"label": "late night", "energy_bias": -0.25, "depth_bias": 0.15}
    if hour < 11:
        return {"label": "morning", "energy_bias": 0.10, "depth_bias": -0.05}
    if hour < 17:
        return {"label": "daytime", "energy_bias": 0.05, "depth_bias": 0.0}
    if hour < 22:
        return {"label": "evening", "energy_bias": -0.05, "depth_bias": 0.05}
    return {"label": "night", "energy_bias": -0.20, "depth_bias": 0.10}
