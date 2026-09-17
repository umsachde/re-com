"""Bodies for server.py's mood tools: recommend_for_mood,
recommend_from_playlist_for_mood, read_my_mood. See each one's counterpart in
server.py for the tool's user-facing contract (docstring); PLAN.md 7.9."""

from typing import Any

import server


def recommend_for_mood(
    feeling: str | None = None,
    vector: dict[str, float] | None = None,
    context: str | None = None,
    arc: str = "mirror",
    limit: int = 20,
    genres: list[str] | None = None,
    language: list[str] | None = None,
    exclude_languages: list[str] | None = None,
    allow_unlabelled_language: bool = False,
    bpm: float | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
) -> dict[str, Any]:
    """See server.recommend_for_mood for the tool contract."""
    import recommend
    import store as _s

    server._require_mood_support()
    yt = server._client()
    conn = server._store()
    # Learn from earlier rounds before ranking this one. Local SQL only, and
    # idempotent, so it's cheap enough to run on every call rather than
    # needing its own cron.
    _s.infer_implicit_feedback(conn)
    exclude = server._library_video_ids(yt) | _s.rejected_video_ids(conn) | server._recently_served()
    graph_conn = server._graph()

    result = recommend.build(
        yt, conn, exclude=exclude, feeling=feeling, vector=vector,
        context=context, arc=arc, limit=limit, genres=genres,
        language=language, exclude_languages=exclude_languages,
        allow_unlabelled_language=allow_unlabelled_language,
        bpm=bpm, bpm_min=bpm_min, bpm_max=bpm_max,
        graph_conn=graph_conn,
        exclude_index=server._library_exclusion_index() if graph_conn else None,
    )
    _s.log_recommendations(conn, result["songs"], result["target"], feeling, arc)
    server._mark_served(result["songs"], "recommend_for_mood")
    return result


def recommend_from_playlist_for_mood(
    playlist_id: str,
    feeling: str | None = None,
    vector: dict[str, float] | None = None,
    context: str | None = None,
    arc: str = "mirror",
    limit: int = 20,
    language: list[str] | None = None,
    exclude_languages: list[str] | None = None,
    allow_unlabelled_language: bool = False,
    bpm: float | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    seed_cap: int | None = None,
) -> dict[str, Any]:
    """See server.recommend_from_playlist_for_mood for the tool contract."""
    import recommend
    import store as _s

    server._require_mood_support()
    yt = server._client()
    conn = server._store()
    _s.infer_implicit_feedback(conn)  # same reasoning as recommend_for_mood

    playlist = yt.get_playlist(playlist_id, limit=None)
    tracks = [t for t in playlist.get("tracks", []) if t.get("videoId")]
    if not tracks:
        raise RuntimeError(f"Playlist {playlist_id!r} has no playable tracks to read.")

    resolved = recommend.resolve_target(conn, yt, feeling, vector, context)
    picked = recommend.pick_seeds_from_playlist(
        conn, tracks, resolved["target"], cap=seed_cap or recommend.PLAYLIST_SEED_CAP
    )
    if not picked["seeds"]:
        raise RuntimeError(
            f"No track in this playlist fits that mood well enough to seed from "
            f"({picked['considered']} considered). Seeding from tracks that don't fit "
            "would just return the playlist's own mood back. Try recommend_for_mood to "
            "draw on the whole library instead, or run scripts/label_library.py if these "
            "tracks are simply unlabelled."
        )

    exclude = (
        server._library_video_ids(yt)
        | _s.rejected_video_ids(conn)
        | server._recently_served()
        | {t["videoId"] for t in tracks}
    )
    graph_conn = server._graph()

    result = recommend.build(
        yt, conn, exclude=exclude, feeling=feeling, vector=vector,
        context=context, arc=arc, limit=limit,
        language=language, exclude_languages=exclude_languages,
        allow_unlabelled_language=allow_unlabelled_language,
        bpm=bpm, bpm_min=bpm_min, bpm_max=bpm_max,
        seeds=picked["seeds"], resolved=resolved,
        graph_conn=graph_conn,
        exclude_index=server._library_exclusion_index() if graph_conn else None,
    )
    result["seed_report"] = {
        "playlist_id": playlist_id,
        "considered": picked["considered"],
        "genuine": picked["genuine"],
        "seeded_from": len(picked["seeds"]),
        "capped": picked["capped"],
    }
    if picked["capped"]:
        result["notes"].append(
            f"{picked['genuine']} of {picked['considered']} playlist tracks fit this mood; "
            f"seeded from the best {len(picked['seeds'])} of them."
        )
    else:
        result["notes"].append(
            f"Seeded from {len(picked['seeds'])} of {picked['considered']} playlist tracks "
            "-- the ones that genuinely fit this mood."
        )

    _s.log_recommendations(conn, result["songs"], result["target"], feeling, arc)
    server._mark_served(result["songs"], "recommend_from_playlist_for_mood")
    return result


def read_my_mood() -> dict[str, Any]:
    """See server.read_my_mood for the tool contract."""
    import moodspace
    import sense

    server._require_mood_support()
    read = sense.read_mood(server._store(), server._client())
    return {
        **read,
        "described": moodspace.describe(read["vector"]) if read["vector"] else None,
    }
