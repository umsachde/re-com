"""MCP server that recommends new songs.

Combines multiple independent discovery signals (radio, related content,
artist catalog expansion) instead of trusting a single algorithm, and always
excludes anything already in the user's library. Backed by YouTube Music or
Spotify, chosen at process start by RECOM_PROVIDER ("youtube", the default,
or "spotify") and reached entirely through a sibling `*-mcp` server
(ytmusic-mcp / spotify-mcp -- see ytmusic_client.py / spotify_client.py) --
re-com holds no streaming-service credentials of its own for either backend.
See provider.py for the shared interface both backends implement, and
PLAN.md for the v3 multi-provider design notes.
"""

import functools
import json
import os
import random
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

import provider as provider_module
from provider import ProviderError
from ytmusic_client import YTMusicClient

# Rebuilding the library exclusion set costs ~20s of API calls (Liked Music
# plus every playlist), and every tool needs it. It's cached on disk instead;
# see _library_video_ids for how staleness is bounded.
# Which backend this server instance talks to. Each provider is a separate
# MCP server instance/registration (e.g. "re-com" for YouTube Music,
# "re-com-spotify" for Spotify) rather than a per-call argument -- a single
# call should stay within one provider, and this keeps that true structurally.
PROVIDER = provider_module.active()

_DEFAULT_CACHE_PATH = Path.home() / ".recom" / "library_cache.json"
# Scoped per backend. An exclusion set is a set of one provider's ids, and
# sharing the file between two instances doesn't merely mix them -- it
# silently voids the guarantee, since no YouTube videoId can ever equal a
# Spotify track id. See provider.scoped_path.
CACHE_PATH = Path(
    os.environ.get("RECOM_CACHE_PATH") or provider_module.scoped_path(_DEFAULT_CACHE_PATH)
)
# Seconds a cached exclusion set stays usable. <= 0 disables caching entirely.
CACHE_TTL = int(os.environ.get("RECOM_CACHE_TTL", 6 * 60 * 60))
# How many of the most recently liked songs to re-check on every cache hit.
# ytmusic-mcp/ytmusicapi pages this, so the real count returned is typically ~2x.
RECENT_LIKES_LIMIT = 100

# The v2 mood engine depends on atlas.py's crawl of YouTube's own mood
# playlists, which no other backend has an equivalent of yet -- Spotify
# forbids reading other users' playlists outright (measured 403). PLAN.md's
# v6 section covers the provider-neutral replacement.
MOOD_PROVIDERS = {"youtube"}

mcp = MCPServer("re-com" if PROVIDER == "youtube" else f"re-com-{PROVIDER}")

_yt: Any = None
_store_conn = None


def _store():
    """Lazily open the v2 mood store, shared across tool calls."""
    global _store_conn
    if _store_conn is None:
        import store

        _store_conn = store.connect()
    return _store_conn


def _require_mood_support() -> None:
    """Refuse mood tools on a backend whose mood index can't exist yet.

    Without this the failure is silent and worse than an error: the mood
    engine reads seeds and candidates out of the local store, so on a
    non-YouTube instance it sent YouTube videoIds to the provider (measured:
    Spotify 400s every one as an invalid base62 id), fell back to atlas
    candidates, and returned *YouTube* ids as that provider's
    recommendations -- against an exclusion set in the wrong id namespace,
    so the novelty guarantee was void too.

    Failing loudly with a reason beats handing back results that look fine
    and are entirely wrong.
    """
    if PROVIDER not in MOOD_PROVIDERS:
        raise RuntimeError(
            f"Mood-based recommendation isn't available on the {PROVIDER!r} backend yet. "
            "It needs a mood index built from that service's own playlists, and only "
            f"YouTube Music has one so far ({PROVIDER} doesn't expose the playlist data "
            "it would take to build). Use recommend_from_song, recommend_from_playlist "
            f"or songs_by_artist on {PROVIDER}, or the 're-com' (YouTube Music) server "
            "for mood. See PLAN.md's v6 section for the provider-neutral replacement."
        )


def _client() -> Any:
    """The (persistent, lazily-started) connection to this instance's
    provider's sibling *-mcp server (ytmusic-mcp or spotify-mcp), selected by
    RECOM_PROVIDER ("youtube", the default, or "spotify").

    re-com never holds streaming-service credentials itself -- the sibling
    *-mcp server owns auth entirely; see ytmusic_client.py / spotify_client.py.
    Both implement the same provider.Provider shape, so nothing past this
    function needs to know which backend it's talking to.
    """
    global _yt
    if _yt is None:
        if PROVIDER == "spotify":
            from spotify_client import SpotifyClient

            _yt = SpotifyClient()
        elif PROVIDER == "youtube":
            _yt = YTMusicClient()
        else:
            raise RuntimeError(
                f"Unknown RECOM_PROVIDER {PROVIDER!r}. Expected 'youtube' or 'spotify'."
            )
    return _yt


def handle_errors(fn):
    """Translate provider/argument-validation failures into clean tool errors.

    Every provider's sibling *-mcp server already turns auth/rate-limit/gated/
    network failures into clear, actionable messages (a ProviderError
    subclass) -- this just keeps those from arriving as raw tracebacks, and
    does the same for our own validation.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ProviderError as e:
            raise RuntimeError(str(e)) from e
        except ValueError as e:
            # Our own argument validation (e.g. an unknown arc name). These are
            # actionable messages already -- they just need to arrive as a
            # clean tool error rather than a raw traceback.
            raise RuntimeError(str(e)) from e

    return wrapper


# Candidate generation lives in signals.py, shared with the mood engine.
# Re-exported here so existing callers and tests keep their import paths.
from signals import (  # noqa: E402
    _RELATED_ARTISTS_TO_EXPAND,
    _SIGNAL_ERRORS,
    _SOURCE_ARTIST,
    _SOURCE_RADIO,
    _SOURCE_RELATED,
    _artist_names_match,
    _filter_same_artist,
    _finalize,
    _gather_seed_candidates,
    _merge_and_score,
    _norm_track,
    gather_seeds,
    same_song,
)

def _liked_video_ids(yt: YTMusicClient) -> set[str]:
    # get_liked_songs() defaults to only the most recent 100 items -- not
    # enough for a real library. get_playlist("LM", limit=None) fetches all.
    liked = yt.get_playlist("LM", limit=None)
    return {t["videoId"] for t in liked.get("tracks", []) if t.get("videoId")}


def _recent_liked_video_ids(yt: YTMusicClient) -> set[str]:
    """The most recently liked songs only -- one page, ~1s instead of ~7s.

    Newly liked songs land at the top of Liked Music, so this is what keeps a
    cached exclusion set honest about the mutation users actually make most.
    """
    recent = yt.get_playlist("LM", limit=RECENT_LIKES_LIMIT)
    return {t["videoId"] for t in recent.get("tracks", []) if t.get("videoId")}


def _build_library_video_ids(yt: YTMusicClient) -> set[str]:
    """Full rebuild: Liked Music plus every playlist the user owns. ~20s."""
    ids = set(_liked_video_ids(yt))
    for pl in yt.get_library_playlists(limit=None):
        playlist_id = pl.get("playlistId")
        if not playlist_id or playlist_id == "LM":
            continue
        try:
            full = yt.get_playlist(playlist_id, limit=None)
        except _SIGNAL_ERRORS:
            continue
        ids |= {t["videoId"] for t in full.get("tracks", []) if t.get("videoId")}
    return ids


def _read_cache() -> tuple[set[str], float] | None:
    """Return (ids, fetched_at) if a usable, unexpired cache exists, else None.

    A cache that is missing, unreadable, malformed, or expired is simply a
    miss -- never an error. The worst case is the ~20s rebuild we already had.
    """
    if CACHE_TTL <= 0:
        return None
    try:
        raw = json.loads(CACHE_PATH.read_text())
        fetched_at = float(raw["fetched_at"])
        ids = {v for v in raw["video_ids"] if isinstance(v, str)}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if time.time() - fetched_at > CACHE_TTL:
        return None
    return ids, fetched_at


def _write_cache(ids: set[str]) -> float:
    """Persist the exclusion set, returning the timestamp recorded for it.

    Written via a temp file + rename so an interrupted write can't leave a
    half-written cache behind. Failure to write is non-fatal -- the caller
    already has the data it needs.
    """
    fetched_at = time.time()
    payload = json.dumps({"fetched_at": fetched_at, "video_ids": sorted(ids)})
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
        tmp.write_text(payload)
        tmp.replace(CACHE_PATH)
    except OSError:
        pass
    return fetched_at


def _library_video_ids(yt: YTMusicClient, force_refresh: bool = False) -> set[str]:
    """Every videoId already in the user's library: Liked Music plus every
    playlist they own, not just one seed playlist.

    Cached on disk (see CACHE_PATH / CACHE_TTL) because a full rebuild costs
    ~20s and every tool call needs it. On a cache hit the most recently liked
    songs are still fetched fresh (~1s) and unioned in, so liking a song takes
    effect immediately rather than waiting out the TTL. Adding a song to some
    other playlist is the case a hit can miss; refresh_library() forces a
    rebuild for that.
    """
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            ids, _ = cached
            try:
                return ids | _recent_liked_video_ids(yt)
            except _SIGNAL_ERRORS:
                # Top-up is an optimization, not a correctness requirement --
                # the cached set is still a valid (if slightly older) answer.
                return ids

    ids = _build_library_video_ids(yt)
    _write_cache(ids)
    return ids


def _resolve_artist(yt: YTMusicClient, artist: str) -> dict[str, Any] | None:
    """Resolve a free-text artist name to a search result carrying its
    channelId (`browseId`), or None if nothing matched."""
    results = yt.search(artist, filter="artists", limit=1)
    return results[0] if results else None


def _resolve_song_video_id(yt: YTMusicClient, song: str, artist: str | None = None) -> str | None:
    """Resolve a free-text song title (optionally with an artist name) to a
    videoId via search, so callers don't need to pre-resolve one themselves.

    Prefers a search result whose artist name matches `artist` (loosely --
    substring match, case-insensitive, since search titles/artist credits
    vary in exact formatting); falls back to the top search hit otherwise.
    """
    query = f"{song} {artist}" if artist else song
    results = yt.search(query, filter="songs", limit=10)
    if not results:
        return None
    if artist:
        artist_lower = artist.lower()
        for r in results:
            names = [a.get("name", "").lower() for a in (r.get("artists") or [])]
            if any(artist_lower in n or n in artist_lower for n in names if n):
                return r.get("videoId")
    return results[0].get("videoId")


def _artist_song_catalog(yt: YTMusicClient, channel_id: str) -> list[dict[str, Any]]:
    """Pull an artist's actual song catalog -- not similarity candidates.

    get_artist()'s own "songs" section is just a short top-songs preview.
    Its browseId points to the artist's full "Songs" playlist on YT Music,
    which is fetched for real catalog depth; the preview is a fallback if
    that lookup is unavailable.
    """
    info = yt.get_artist(channel_id)
    songs = info.get("songs") or {}
    browse_id = songs.get("browseId")
    if browse_id:
        try:
            full = yt.get_playlist(browse_id, limit=None)
        except _SIGNAL_ERRORS:
            full = None
        if full and full.get("tracks"):
            return full["tracks"]
    return songs.get("results") or []


# --- tools -------------------------------------------------------------


def _apply_result_filters(
    ranked: list[dict[str, Any]],
    seed_video_id: str | None,
    seed_title: str | None,
    seed_artist: str | None,
    limit: int,
    language=None,
    exclude_languages=None,
    allow_unlabelled_language: bool = False,
    bpm: float | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    match_seed_tempo: bool = False,
    expand_across_language: bool = True,
    max_per_artist: int = 2,
) -> dict[str, Any]:
    """Apply language and tempo filters to an already-ranked result list.

    Shared by the similarity tools so a filter behaves identically no matter
    which tool asked for it.
    """
    import filters
    import recommend as _r
    import signals

    conn = _store()
    notes: list[str] = []

    if match_seed_tempo and bpm is None and seed_video_id:
        track = _store_track(conn, seed_video_id, seed_title, seed_artist)
        bpm = filters.seed_tempo(conn, seed_video_id, track["title"], track["artists"])
        if bpm:
            notes.append(f"Seed tempo is {bpm:.0f}bpm; results biased toward it.")
        else:
            notes.append("Seed tempo unknown (Deezer has no BPM for it), so tempo was not used.")

    # Drop re-uploads of the seed itself. The videoId exclusion in
    # _gather_seed_candidates can't see them: the same song exists many times
    # over on YouTube under different ids.
    seed_track = _store_track(conn, seed_video_id, seed_title, seed_artist) if seed_video_id else {}
    seed_name = seed_track.get("title") or seed_title
    seed_credit = seed_track.get("artists") or seed_artist
    if seed_name:
        ranked = [
            song for song in ranked
            if not signals.same_song(seed_name, seed_credit, song.get("title"),
                                     " & ".join(song.get("artists") or []) or None)
        ]

    for song in ranked:
        song["base_score"] = song.get("score", 1)

    filtered, language_report = filters.apply_language(
        conn, ranked, want=language, exclude=exclude_languages,
        allow_unlabelled=allow_unlabelled_language,
    )
    if language_report["applied"]:
        # Filtering a pool the seed's own language dominates leaves very little
        # behind, so re-seed from the songs that did pass rather than handing
        # back a short list.
        if len(filtered) < limit and expand_across_language:
            filtered, added = _r.bridge_expand(
                _client(), conn, filtered, exclude=set(), want=language,
                exclude_languages=exclude_languages,
                allow_unlabelled=allow_unlabelled_language, needed=limit,
            )
            if added:
                notes.append(
                    f"Only {language_report['kept']} of the seed's own results were in the "
                    f"requested language, so I re-seeded from those and found {added} more."
                )
                language_report = {**language_report, "kept": len(filtered), "bridge_added": added}
        notes.append(_r._language_note(language_report, limit))

    filtered, tempo_report = filters.apply_tempo(
        conn, filtered, target_bpm=bpm, bpm_min=bpm_min, bpm_max=bpm_max
    )
    if tempo_report["applied"]:
        notes.append(_r._tempo_note(tempo_report))

    filtered.sort(key=lambda c: (-c.get("base_score", 0), c.get("title") or ""))

    # Cap per artist. The mood path gets this from the sequencer; without it
    # here, one prolific artist fills the whole result -- a language filter made
    # that obvious by returning four DIVINE tracks out of eight.
    picked, per_artist = [], {}
    for song in filtered:
        key = ((song.get("artists") or [None])[0] or "").lower()
        if key and per_artist.get(key, 0) >= max_per_artist:
            continue
        if key:
            per_artist[key] = per_artist.get(key, 0) + 1
        song.pop("base_score", None)
        picked.append(song)
        if len(picked) >= limit:
            break

    return {
        "songs": picked,
        "notes": notes,
        "filters": {"language": language_report, "tempo": tempo_report},
    }


def _store_track(conn, video_id: str, title: str | None, artist: str | None) -> dict[str, Any]:
    """Best-known identity for a seed, preferring what the store already has."""
    import store as _s

    known = _s.get_track(conn, video_id) or {}
    return {"title": known.get("title") or title, "artists": known.get("artists") or artist}


@mcp.tool()
@handle_errors
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
    """Recommend new songs similar to a seed song.

    Seed the search either with a known `video_id`, or with `song` (a free-text
    title, optionally narrowed with `artist`) to have the seed resolved via
    search internally -- e.g. "10 songs that relate to Kryptonite by 3 Doors
    Down" needs no separate lookup first. Exactly one of `video_id` or `song`
    must be given.

    Combines YouTube Music's radio, its separate "related" signal, and the
    seed artist's own catalog plus related artists' catalogs, then ranks by
    how many independent signals agreed on each candidate. Never returns the
    seed song itself, and never returns a song already in Liked Music or in
    ANY of the user's playlists.

    By default candidates can come from OTHER artists too (radio/related
    signals surface stylistically similar tracks, not just the seed artist's
    own catalog) -- pass `same_artist_only=True` to keep only songs credited
    to the seed's own artist(s), e.g. for "recommend songs BY artist X similar
    to song Y" requests.

    `language` / `exclude_languages` filter the RESULTS independently of the
    seed, which is the point: seeding from a Punjabi song with
    language=["english"] returns English songs similar to it. Strict by
    default -- candidates with no language label are dropped, since a
    Punjabi-seeded pool is mostly Punjabi and keeping unlabelled ones would
    hand back exactly what was excluded. Pass allow_unlabelled_language=True
    to relax that; the response always reports what was dropped.

    When a language filter leaves too few results -- seeding from a Punjabi
    song and asking for English usually does -- the surviving songs are used as
    fresh seeds to reach more of that language in the same neighbourhood, since
    filtering alone can only return what happened to be in the seed's own pool.
    Set expand_across_language=False to skip that and get the short list.

    `match_seed_tempo=True` biases results toward the seed's own BPM (half-
    and double-time count as close). `bpm` sets a tempo target directly, and
    `bpm_min`/`bpm_max` bound it. Songs with no known BPM are kept and simply
    not scored on tempo.

    Returns {"songs": [...], "notes": [...], "filters": {...}} -- notes carry
    anything the user should hear about, such as results dropped for having no
    language label.

    The library exclusion set is cached for speed; newly liked songs are
    always honoured, but call refresh_library() after adding songs to a
    playlist by other means.
    """
    yt = _client()
    if not video_id:
        if not song:
            raise RuntimeError("Provide either video_id or song (optionally with artist).")
        video_id = _resolve_song_video_id(yt, song, artist)
        if video_id is None:
            desc = f"{song!r} by {artist!r}" if artist else repr(song)
            raise RuntimeError(f"No song found matching {desc}.")

    seed_artist_names: list[str] = []
    candidates = _gather_seed_candidates(yt, video_id, seed_artist_names)
    merged = _merge_and_score([candidates])
    if same_artist_only:
        merged = _filter_same_artist(merged, seed_artist_names or ([artist] if artist else []))

    exclude = _library_video_ids(yt)
    ranked, variants_collapsed = _finalize(merged, exclude, limit * 12 if (language or exclude_languages or bpm or bpm_min or bpm_max or match_seed_tempo) else limit)

    result = _apply_result_filters(
        ranked, seed_video_id=video_id, seed_title=song, seed_artist=artist,
        limit=limit, language=language, exclude_languages=exclude_languages,
        allow_unlabelled_language=allow_unlabelled_language,
        bpm=bpm, bpm_min=bpm_min, bpm_max=bpm_max, match_seed_tempo=match_seed_tempo,
        expand_across_language=expand_across_language, max_per_artist=max_per_artist,
    )
    if variants_collapsed:
        result["notes"].insert(
            0, f"Collapsed {variants_collapsed} remix/feature variant(s) down to one per song."
        )
    return result


@mcp.tool()
@handle_errors
def recommend_from_playlist(playlist_id: str, limit: int = 20, seed_sample_size: int = 5) -> list[dict[str, Any]]:
    """Recommend new songs based on an entire playlist.

    Randomly samples up to seed_sample_size tracks from the playlist as seeds
    (the whole playlist if it's smaller), runs the same multi-signal
    candidate generation as recommend_from_song for each, and pools/ranks the
    results. Never returns a song already in Liked Music, already in the
    source playlist, or already in ANY other of the user's playlists.

    The library exclusion set is cached for speed; newly liked songs are
    always honoured, but call refresh_library() after adding songs to a
    playlist by other means.
    """
    yt = _client()
    playlist = yt.get_playlist(playlist_id, limit=None)
    tracks = [t for t in playlist.get("tracks", []) if t.get("videoId")]
    if not tracks:
        return []

    sample = tracks if len(tracks) <= seed_sample_size else random.sample(tracks, seed_sample_size)

    # skip_failures=False keeps this tool's existing contract: a seed that
    # fails here surfaces as a clear error via handle_errors rather than
    # quietly shrinking the candidate pool.
    per_seed = gather_seeds(yt, [t["videoId"] for t in sample], skip_failures=False)
    merged = _merge_and_score(per_seed)

    exclude = _library_video_ids(yt) | {t["videoId"] for t in tracks}
    songs, _ = _finalize(merged, exclude, limit)
    return songs


@mcp.tool()
@handle_errors
def songs_by_artist(artist: str, limit: int = 10) -> dict[str, Any]:
    """Return actual songs by a specific artist -- a direct catalog pull, not
    a similarity recommendation like recommend_from_song/recommend_from_playlist.

    Resolves `artist` (a name) to its YouTube Music channel and pulls its
    real song catalog, excluding anything already in Liked Music OR in ANY
    of the user's playlists (not just one, unlike recommend_from_playlist's
    single-seed-playlist exclusion). Read-only: never adds results anywhere.

    This is a hard requirement, not best-effort -- if fewer than `limit`
    qualifying songs exist after exclusion, this returns however many were
    actually found rather than padding the list. Check `found` vs
    `requested` in the result to see whether it fell short.

    Remix/feature variants of the same underlying song (e.g. a track and its
    "(feat. ...)" credit under a different videoId) count once, not once
    per variant -- see `variants_collapsed` in the result.

    The library exclusion set is cached for speed; newly liked songs are
    always honoured, but call refresh_library() after adding songs to a
    playlist by other means.
    """
    yt = _client()
    resolved = _resolve_artist(yt, artist)
    if resolved is None or not resolved.get("browseId"):
        return {"artist": None, "requested": limit, "found": 0, "variants_collapsed": 0, "songs": []}

    catalog = _artist_song_catalog(yt, resolved["browseId"])
    exclude = _library_video_ids(yt)

    songs: list[dict[str, Any]] = []
    seen: set[str] = set()
    variants_collapsed = 0
    for item in catalog:
        track = _norm_track(item)
        vid = track["videoId"]
        if not vid or vid in exclude or vid in seen:
            continue
        track_artist = (track.get("artists") or [None])[0]
        if any(
            same_song(track["title"], track_artist, s["title"], (s.get("artists") or [None])[0])
            for s in songs
        ):
            seen.add(vid)
            variants_collapsed += 1
            continue
        seen.add(vid)
        songs.append(track)
        if len(songs) >= limit:
            break

    return {
        "artist": resolved.get("artist"),
        "requested": limit,
        "found": len(songs),
        "variants_collapsed": variants_collapsed,
        "songs": songs,
    }


@mcp.tool()
@handle_errors
def refresh_library() -> dict[str, Any]:
    """Rebuild the cached library exclusion set from scratch, right now.

    Every recommendation tool excludes songs already in Liked Music or any of
    your playlists. That set is expensive to build (~20s), so it's cached and
    reused. Liking a song is picked up immediately regardless, but adding a
    song to some other playlist is only seen once the cache is rebuilt.

    Call this after adding songs to a playlist by other means (e.g. a
    playlist-management tool) if you want the next recommendation to account
    for them without waiting out the cache TTL.
    """
    yt = _client()
    ids = _library_video_ids(yt, force_refresh=True)
    return {
        "tracks_excluded": len(ids),
        "cache_path": str(CACHE_PATH),
        "ttl_seconds": CACHE_TTL,
    }


# --- v2: mood ---------------------------------------------------------------


@mcp.tool()
@handle_errors
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
    """Recommend new songs that match how the listener actually feels right now.

    Unlike recommend_from_song, the mood decides where candidates come from:
    seeds are drawn from the listener's OWN library nearest the target mood,
    then expanded through radio/related/artist signals and songs from YouTube's
    mood playlists. Results are still guaranteed absent from their library.

    Describing the mood -- in priority order:
      `vector`  The precise path, and the one to prefer. A dict with
                valence (-1..1, despairing->euphoric), energy (0..1,
                still->frantic), tension (0..1, resolved->anxious; this is what
                separates angry from excited) and depth (0..1, background
                ->lyric-forward). YOU should read the user's words and set
                these -- you understand "wistful but still wants to get things
                done" far better than any keyword list.
      `feeling` Their words verbatim, as a fallback when you'd rather not
                commit to numbers. Matched against a mood-word lexicon.
      `context` One of: Chill, Sleep, Focus, Commute, Feel good, Romance,
                Energize, Workout, Party, Gaming, Sad.
      If none are given, the mood is inferred from recent listening history.

    `arc` shapes the sequence rather than returning a flat mood-matched set:
      mirror  stay where they are and validate it (default)
      lift    start where they are, rise gradually -- never jump straight to
              upbeat when someone is low, it reads as being told to cheer up
      settle  descend to calm; an evening wind-down
      deepen  go further in; sometimes you want to sit in it properly
      hold    stay in a band with energy as a curve (workout: warmup/peak/cooldown)

    `genres` optionally restricts the seeds to the listener's own genre
    playlists, e.g. ["Punjabi", "Hip-Hop & Rap"].

    `language` / `exclude_languages` filter the RESULTS, e.g.
    language=["english"]. Strict by default: a candidate with no language label
    is dropped, because someone asking for English only wants a guarantee. The
    response says how many were dropped and why; pass
    allow_unlabelled_language=True to keep them.

    `bpm` biases ranking toward a tempo (half- and double-time count as close).
    `bpm_min`/`bpm_max` bound it instead. Songs with no known BPM are KEPT and
    simply not scored on tempo -- Deezer has no tempo for much of the
    non-English catalogue, so dropping them would delete whole languages.

    `limit` is a ceiling, not a guarantee. If fewer than `limit` songs genuinely
    fit the mood (rated, with a real fit -- not just an unrated placeholder
    score), the shortfall is NOT padded with weak filler to hit the number.
    Filler is capped at 25% of `limit`: asking for 100 with 7 genuine matches
    returns 32 (7 + 25), not 100. See `match_quality` in the result for the
    genuine/filler breakdown, and `notes` for the human-readable version.

    If they also point at a specific playlist ("look at this playlist and
    recommend me songs for how I feel"), use recommend_from_playlist_for_mood
    instead -- it seeds from that playlist's own fitting tracks rather than
    from the whole library.

    This is READ-ONLY. If they asked for a PLAYLIST rather than a list, this
    tool is step one of three: get the songs here, create the playlist from the
    returned videoIds with a playlist-management tool, then call
    refresh_library() so those tracks are excluded from later recommendations.

    The result carries `target` (the mood aimed at), `target_origin` (where it
    came from), `seeds` (which of their songs it grew from), `notes` (caveats
    worth repeating to the user), `match_quality` (genuine vs. filler counts)
    and `songs`, each with its slot, mood fit and which signals surfaced it.
    """
    import recommend
    import store as _s

    _require_mood_support()
    yt = _client()
    conn = _store()
    # Learn from earlier rounds before ranking this one. Local SQL only, and
    # idempotent, so it's cheap enough to run on every call rather than
    # needing its own cron.
    _s.infer_implicit_feedback(conn)
    exclude = _library_video_ids(yt) | _s.rejected_video_ids(conn)

    result = recommend.build(
        yt, conn, exclude=exclude, feeling=feeling, vector=vector,
        context=context, arc=arc, limit=limit, genres=genres,
        language=language, exclude_languages=exclude_languages,
        allow_unlabelled_language=allow_unlabelled_language,
        bpm=bpm, bpm_min=bpm_min, bpm_max=bpm_max,
    )
    _s.log_recommendations(conn, result["songs"], result["target"], feeling, arc)
    return result


@mcp.tool()
@handle_errors
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
    """Recommend new songs from ONE playlist, shaped by how the listener feels.

    For "I feel like this -- look at this playlist and find me songs". Use this
    over recommend_from_playlist whenever a mood is part of the ask, and over
    recommend_for_mood whenever a specific playlist is.

    Unlike recommend_from_playlist, which samples a few tracks at random and
    ignores mood entirely: EVERY track in the playlist is read and scored for
    mood fit, and only genuine matches -- tracks whose own mood resolves and
    actually fits the target -- are used as seeds. An off-mood playlist
    therefore yields few seeds or none, which is reported rather than papered
    over by seeding from tracks that don't fit.

    Seeding costs ~4 API calls per seed, so the best-fitting seeds are capped
    (default 20, override with `seed_cap`). `seed_report` in the result says how
    many tracks were considered, how many were genuine, and how many were
    capped away.

    Mood arguments behave exactly as in recommend_for_mood (`vector` preferred,
    then `feeling`, then `context`; falls back to inferred mood). `arc` shapes
    the sequence the same way. Results are guaranteed absent from Liked Music,
    from this playlist, and from every other playlist -- and `limit` is a
    ceiling, not a guarantee: filler is capped at 25% of it, same as
    recommend_for_mood.

    This is READ-ONLY -- it never creates a playlist or adds anything anywhere.
    To turn the result into a real playlist, pass the returned videoIds to a
    playlist-management tool, then call refresh_library() so the new tracks are
    excluded from later recommendations.
    """
    import recommend
    import store as _s

    _require_mood_support()
    yt = _client()
    conn = _store()
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
        _library_video_ids(yt)
        | _s.rejected_video_ids(conn)
        | {t["videoId"] for t in tracks}
    )

    result = recommend.build(
        yt, conn, exclude=exclude, feeling=feeling, vector=vector,
        context=context, arc=arc, limit=limit,
        language=language, exclude_languages=exclude_languages,
        allow_unlabelled_language=allow_unlabelled_language,
        bpm=bpm, bpm_min=bpm_min, bpm_max=bpm_max,
        seeds=picked["seeds"], resolved=resolved,
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
    return result


@mcp.tool()
@handle_errors
def read_my_mood() -> dict[str, Any]:
    """Infer the listener's current mood from recent listening, with evidence.

    Returns the inferred `vector`, a plain-language `described`, a `confidence`,
    and `evidence` -- the specific observations behind it (a song on repeat, one
    artist dominating, valence drifting across the session).

    Lead with the evidence, not the verdict. "You've had these three on loop
    since yesterday -- want something that sits there with you, or something
    that lifts?" is the point of this tool; asserting "you are sad" is not.
    Mood inference is often wrong, so offer it as a read the user can correct.
    """
    import moodspace
    import sense

    _require_mood_support()
    read = sense.read_mood(_store(), _client())
    return {
        **read,
        "described": moodspace.describe(read["vector"]) if read["vector"] else None,
    }


@mcp.tool()
@handle_errors
def explain_recommendation(video_id: str) -> dict[str, Any]:
    """Explain why a song was recommended, in mood terms.

    Reports the song's mood vector, which layer produced it (Claude reading the
    lyrics, YouTube mood-playlist membership, or the artist's own average), the
    named moods it sits closest to, and the mood it was last served against.
    """
    import label
    import moodspace
    import store as _s

    conn = _store()
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


@mcp.tool()
@handle_errors
def record_feedback(video_id: str, reaction: str) -> dict[str, Any]:
    """Record what the listener thought of a recommendation.

    `reaction` is one of: loved, saved, skipped, wrong_mood.

    `wrong_mood` is the valuable one -- it says the song was fine but the mood
    read was off, which is a different failure from simply not liking it.
    Anything marked skipped or wrong_mood is never recommended again.
    """
    import store as _s

    allowed = {"loved", "saved", "skipped", "wrong_mood"}
    if reaction not in allowed:
        raise RuntimeError(f"reaction must be one of: {', '.join(sorted(allowed))}.")

    _s.put_feedback(_store(), video_id, reaction)
    return {"videoId": video_id, "reaction": reaction, "recorded": True}


@mcp.tool()
@handle_errors
def index_status() -> dict[str, Any]:
    """Report how much of the mood index exists, so gaps are visible not silent.

    Covers the mood-playlist crawl, how much of the listener's library carries a
    mood label and from which layer, and whether Claude-based labelling is
    configured. Low coverage means recommendations are ranking mostly on signal
    agreement rather than on mood -- worth saying out loud.
    """
    import judge
    import label
    import store as _s

    conn = _store()
    _s.infer_implicit_feedback(conn)
    return {
        # Name the backend and its store: these numbers describe one
        # provider's index, and an empty one on a fresh backend is a real
        # answer rather than a bug.
        "provider": PROVIDER,
        "store": str(_s.DB_PATH),
        "mood_supported": PROVIDER in MOOD_PROVIDERS,
        "atlas": _s.atlas_stats(conn),
        "library": label.library_coverage(conn),
        "feedback": _s.feedback_stats(conn),
        "llm_labelling": {
            "available": judge.available(),
            "model": judge.MODEL,
            "hint": None if judge.available() else "Install with: pip install -e '.[llm]' and run 'ant auth login'.",
        },
    }


if __name__ == "__main__":
    mcp.run()
