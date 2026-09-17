"""Bodies for server.py's similarity-based tools: recommend_from_song,
recommend_from_playlist, songs_by_artist. See each one's counterpart in
server.py for the tool's user-facing contract (docstring); PLAN.md 7.9."""

import random
from typing import Any

import server


def recommend_from_song(
    video_id: str | None = None,
    song: str | None = None,
    artist: str | None = None,
    limit: int = 20,
    same_artist_only: bool = False,
    language: list[str] | None = None,
    exclude_languages: list[str] | None = None,
    allow_unlabelled_language: bool = False,
    bpm: float | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    match_seed_tempo: bool = False,
    expand_across_language: bool = True,
    max_per_artist: int = 2,
) -> dict[str, Any]:
    """See server.recommend_from_song for the tool contract."""
    yt = server._client()
    if not video_id:
        if not song:
            raise RuntimeError("Provide either video_id or song (optionally with artist).")
        video_id = server._resolve_song_video_id(yt, song, artist)
        if video_id is None:
            desc = f"{song!r} by {artist!r}" if artist else repr(song)
            raise RuntimeError(f"No song found matching {desc}.")

    seed_artist_names: list[str] = []
    graph_conn = server._graph()
    seed_meta = {"title": song, "artists": [artist] if artist else []} if song else None
    candidates = server._gather_seed_candidates(
        yt, video_id, seed_artist_names, graph_conn=graph_conn, seed_meta=seed_meta
    )
    merged = server._merge_and_score([candidates])
    if same_artist_only:
        merged = server._filter_same_artist(merged, seed_artist_names or ([artist] if artist else []))

    exclude = server._library_video_ids(yt) | server._recently_served()
    filtering = bool(language or exclude_languages or bpm or bpm_min or bpm_max or match_seed_tempo)
    # The pool stays deep for native candidates while the searching stays
    # bounded -- both numbers together, from one place. See resolve_budgets.
    pool, searches = server.resolve_budgets(limit, filtering=filtering)
    ranked, variants_collapsed = server._finalize(
        merged, exclude, pool, exclude_index=server._library_exclusion_index() if graph_conn else None
    )
    ranked, unresolved = server.resolve_candidates(yt, ranked, pool, exclude, max_resolve=searches)

    result = server._apply_result_filters(
        ranked, seed_video_id=video_id, seed_title=song, seed_artist=artist,
        limit=limit, language=language, exclude_languages=exclude_languages,
        allow_unlabelled_language=allow_unlabelled_language,
        bpm=bpm, bpm_min=bpm_min, bpm_max=bpm_max, match_seed_tempo=match_seed_tempo,
        expand_across_language=expand_across_language, max_per_artist=max_per_artist,
        exclude=exclude,
    )
    if variants_collapsed:
        result["notes"].insert(
            0, f"Collapsed {variants_collapsed} remix/feature variant(s) down to one per song."
        )
    if unresolved:
        result["notes"].append(
            f"Dropped {unresolved} music-graph candidate(s) that couldn't be matched to a "
            f"song on {server.PROVIDER} (or turned out to be in your library after matching)."
        )
    server._mark_served(result["songs"], "recommend_from_song")
    return result


def recommend_from_playlist(
    playlist_id: str, limit: int = 20, seed_sample_size: int = 5
) -> list[dict[str, Any]]:
    """See server.recommend_from_playlist for the tool contract."""
    yt = server._client()
    playlist = yt.get_playlist(playlist_id, limit=None)
    tracks = [t for t in playlist.get("tracks", []) if t.get("videoId")]
    if not tracks:
        # Was a bare `return []`, which is indistinguishable from "there is
        # nothing new to recommend from this playlist" -- two very different
        # things. Measured on Spotify, where the post-Nov-2024 restriction 403s
        # every playlist read: this returned an empty list in 1.5s and said
        # nothing about why. Its sibling recommend_from_playlist_for_mood
        # already raised here; matching it rather than inventing a second
        # answer to the same question.
        raise RuntimeError(f"Playlist {playlist_id!r} has no playable tracks to read.")

    sample = tracks if len(tracks) <= seed_sample_size else random.sample(tracks, seed_sample_size)

    # skip_failures=False keeps this tool's existing contract: a seed that
    # fails here surfaces as a clear error via handle_errors rather than
    # quietly shrinking the candidate pool.
    graph_conn = server._graph()
    per_seed = server.gather_seeds(
        yt,
        [t["videoId"] for t in sample],
        skip_failures=False,
        graph_conn=graph_conn,
        seed_meta={t["videoId"]: server._norm_track(t) for t in sample},
    )
    merged = server._merge_and_score(per_seed)

    exclude = server._library_video_ids(yt) | server._recently_served() | {t["videoId"] for t in tracks}
    pool, searches = server.resolve_budgets(limit)
    songs, _ = server._finalize(
        merged, exclude, pool, exclude_index=server._library_exclusion_index() if graph_conn else None
    )
    songs, _unresolved = server.resolve_candidates(yt, songs, limit, exclude, max_resolve=searches)
    server._mark_served(songs, "recommend_from_playlist")
    return songs


def songs_by_artist(artist: str, limit: int = 10) -> dict[str, Any]:
    """See server.songs_by_artist for the tool contract."""
    yt = server._client()
    resolved = server._resolve_artist(yt, artist)
    if resolved is None or not resolved.get("browseId"):
        return {"artist": None, "requested": limit, "found": 0, "variants_collapsed": 0, "songs": []}

    catalog = server._artist_song_catalog(yt, resolved["browseId"])
    exclude = server._library_video_ids(yt) | server._recently_served()

    songs: list[dict[str, Any]] = []
    seen: set[str] = set()
    variants_collapsed = 0
    for item in catalog:
        track = server._norm_track(item)
        vid = track["videoId"]
        if not vid or vid in exclude or vid in seen:
            continue
        track_artist = (track.get("artists") or [None])[0]
        if any(
            server.same_song(track["title"], track_artist, s["title"], (s.get("artists") or [None])[0])
            for s in songs
        ):
            seen.add(vid)
            variants_collapsed += 1
            continue
        seen.add(vid)
        songs.append(track)
        if len(songs) >= limit:
            break

    server._mark_served(songs, "songs_by_artist")
    return {
        "artist": resolved.get("artist"),
        "requested": limit,
        "found": len(songs),
        "variants_collapsed": variants_collapsed,
        "songs": songs,
    }
