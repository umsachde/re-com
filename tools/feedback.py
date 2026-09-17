"""Bodies for server.py's explain_recommendation and record_feedback tools.
See each one's counterpart in server.py for the tool's user-facing contract
(docstring); PLAN.md 7.9."""

from typing import Any

import server


def explain_recommendation(video_id: str) -> dict[str, Any]:
    """See server.explain_recommendation for the tool contract."""
    import label
    import moodspace
    import store as _s

    conn = server._store()
    track = _s.get_track(conn, video_id) or {}
    entry = label.resolve(conn, video_id)

    served = conn.execute(
        "SELECT served_at, feeling, arc, slot, valence, energy, tension, depth "
        "FROM recommendation WHERE video_id = ? ORDER BY served_at DESC LIMIT 1",
        (video_id,),
    ).fetchone()

    return {
        "videoId": video_id,
        "title": track.get("title"),
        "artists": track.get("artists"),
        "mood": entry["vector"] if entry else None,
        "mood_source": entry["source"] if entry else None,
        "confidence": entry["confidence"] if entry else None,
        "described": moodspace.describe(entry["vector"]) if entry else None,
        "closest_moods": [name for name, _ in moodspace.nearest_anchors(entry["vector"], 3)] if entry else [],
        "atlas_playlists": _s.atlas_moods_for(conn, video_id)[:8],
        "genre": label.genre_prior(conn, video_id),
        "last_served_against": dict(served) if served else None,
    }


def record_feedback(video_id: str, reaction: str) -> dict[str, Any]:
    """See server.record_feedback for the tool contract."""
    import store as _s

    allowed = {"loved", "saved", "skipped", "wrong_mood"}
    if reaction not in allowed:
        raise RuntimeError(f"reaction must be one of: {', '.join(sorted(allowed))}.")

    _s.put_feedback(server._store(), video_id, reaction)
    return {"videoId": video_id, "reaction": reaction, "recorded": True}
