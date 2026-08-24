"""Candidate generation: the multi-signal discovery core.

Extracted from server.py so that both v1's similarity tools and v2's mood
recommendations draw candidates the same way. The ranking on top differs; the
way songs are found does not.

Three independent signals per seed -- YouTube's radio, its separate "related"
feed, and the seed artist's own catalogue plus adjacent artists. A candidate's
score is how many distinct (seed, signal) pairs surfaced it, which makes
agreement between independent signals the thing that ranks, rather than trust
in any one algorithm.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from provider import ProviderError

_SOURCE_RADIO = "radio"
_SOURCE_RELATED = "related"
_SOURCE_ARTIST = "artist"
_RELATED_ARTISTS_TO_EXPAND = 2  # how many of the seed artist's related artists to also pull top songs from

# How many seeds to gather concurrently. Capped rather than unbounded: a
# playlist-seeded mood request can carry 20 seeds, and 20 x ~4 in-flight calls
# is exactly the rate-limit exposure PLAN.md warned about. Six keeps the
# common case (SEED_COUNT seeds) fully parallel while bounding the worst one.
SEED_WORKERS = int(os.environ.get("RECOM_SEED_WORKERS", "6"))

# Every provider (ytmusic-mcp, spotify-mcp, ...) already translates its own
# auth/rate-limit/gated/network failures into a ProviderError subclass with a
# clear, actionable message, so a failed signal for one seed is just this --
# regardless of which backend raised it.
_SIGNAL_ERRORS = (ProviderError,)


def _norm_track(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize the differing track shapes returned by watch playlists,
    related content, and artist song lists into one consistent record."""
    artists = item.get("artists")
    if artists:
        names = [a.get("name") for a in artists if a.get("name")]
    elif item.get("artist"):
        names = [item["artist"]]
    else:
        names = []

    album = item.get("album")
    if isinstance(album, dict):
        album_name = album.get("name")
    elif isinstance(album, str):
        album_name = album
    else:
        album_name = None

    return {
        "videoId": item.get("videoId"),
        "title": item.get("title"),
        "artists": names,
        "album": album_name,
    }


def _gather_seed_candidates(
    yt: Any, seed_video_id: str, seed_artist_names: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Pull radio + related + artist-expansion candidates for one seed song.

    Returns videoId -> {normalized track fields..., "sources": set[str]}.
    A signal that fails is skipped rather than aborting the whole seed.

    If `seed_artist_names` is passed, the seed track's own artist name(s) are
    appended to it as a side effect -- lets callers recover the seed's artist
    without a second lookup, e.g. to filter candidates down to that artist.
    """
    found: dict[str, dict[str, Any]] = {}

    def add(item: dict[str, Any], source: str) -> None:
        vid = item.get("videoId")
        if not vid or vid == seed_video_id:
            return
        if vid not in found:
            found[vid] = {**_norm_track(item), "sources": set()}
        found[vid]["sources"].add(source)

    watch = None
    try:
        watch = yt.get_watch_playlist(videoId=seed_video_id, limit=25, radio=True)
    except _SIGNAL_ERRORS:
        pass

    seed_artist_id = None
    if watch:
        for t in watch.get("tracks", []):
            add(t, _SOURCE_RADIO)
            if t.get("videoId") == seed_video_id and t.get("artists"):
                seed_artist_id = t["artists"][0].get("id")
                if seed_artist_names is not None:
                    seed_artist_names.extend(a.get("name") for a in t["artists"] if a.get("name"))

        related_browse_id = watch.get("related")
        if related_browse_id:
            try:
                sections = yt.get_song_related(related_browse_id)
            except _SIGNAL_ERRORS:
                sections = []
            for section in sections:
                for item in section.get("contents") or []:
                    if isinstance(item, dict) and item.get("videoId"):
                        add(item, _SOURCE_RELATED)

    if seed_artist_id:
        artist = None
        try:
            artist = yt.get_artist(seed_artist_id)
        except _SIGNAL_ERRORS:
            pass
        if artist:
            for s in (artist.get("songs") or {}).get("results", []):
                add(s, _SOURCE_ARTIST)
            related_artists = (artist.get("related") or {}).get("results", [])
            for rel in related_artists[:_RELATED_ARTISTS_TO_EXPAND]:
                rel_id = rel.get("browseId")
                if not rel_id:
                    continue
                try:
                    rel_artist = yt.get_artist(rel_id)
                except _SIGNAL_ERRORS:
                    continue
                for s in (rel_artist.get("songs") or {}).get("results", []):
                    add(s, _SOURCE_ARTIST)

    return found


def gather_seeds(
    yt: Any,
    seed_video_ids: Any,
    *,
    skip_failures: bool = True,
    max_workers: int | None = None,
) -> list[dict[str, dict[str, Any]]]:
    """Gather candidates for several seeds at once.

    Each seed costs ~4 sequential network round-trips and no seed depends on
    any other -- _merge_and_score just pools them -- so running them serially
    made a six-seed mood request take the sum of all six (~18s measured)
    instead of roughly its slowest one.

    Safe to thread because a Provider is a synchronous facade over a single
    asyncio loop on its own thread (see ytmusic_client.YTMusicClient): calls
    are submitted with run_coroutine_threadsafe and the MCP session
    multiplexes concurrent requests over stdio by request id, so N caller
    threads blocking on N in-flight calls is the shape it's already built for.

    Results keep the order of `seed_video_ids` so a run stays reproducible.
    With `skip_failures` a dead seed is dropped rather than sinking the whole
    request; without it the first failure propagates, which is what the v1
    similarity tools want (a seed that fails there is the *only* seed).
    """
    ids = [vid for vid in seed_video_ids if vid]
    if not ids:
        return []

    # One seed doesn't need a pool, and staying on this thread keeps the
    # single-seed path (recommend_from_song) exactly as it was.
    if len(ids) == 1:
        try:
            return [_gather_seed_candidates(yt, ids[0])]
        except Exception:  # noqa: BLE001 - honour skip_failures uniformly
            if not skip_failures:
                raise
            return []

    workers = max_workers or min(len(ids), max(1, SEED_WORKERS))
    out: list[dict[str, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recom-seed") as pool:
        futures = [pool.submit(_gather_seed_candidates, yt, vid) for vid in ids]
        for future in futures:
            try:
                out.append(future.result())
            except Exception:  # noqa: BLE001 - one dead seed must not sink the request
                if not skip_failures:
                    raise
    return out


def _merge_and_score(per_seed: list[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Combine candidates from multiple seeds. Score = number of distinct
    (seed, source) pairs that surfaced each candidate."""
    merged: dict[str, dict[str, Any]] = {}
    for found in per_seed:
        for vid, data in found.items():
            entry = merged.get(vid)
            if entry is None:
                entry = {
                    "videoId": data["videoId"],
                    "title": data["title"],
                    "artists": data["artists"],
                    "album": data["album"],
                    "sources": set(),
                    "score": 0,
                }
                merged[vid] = entry
            entry["sources"] |= data["sources"]
            entry["score"] += len(data["sources"])
    return merged


def _artist_names_match(candidate_artists: list[str], target_names_lower: list[str]) -> bool:
    """Loose match (case-insensitive substring, either direction) between a
    candidate's artist credits and a set of target names -- mirrors the
    matching already used in _resolve_song_video_id, since search/catalog
    artist-name formatting varies (features, "&" vs "feat.", etc)."""
    for name in candidate_artists:
        if not name:
            continue
        name_lower = name.lower()
        if any(t in name_lower or name_lower in t for t in target_names_lower):
            return True
    return False


def _filter_same_artist(merged: dict[str, dict[str, Any]], target_names: list[str]) -> dict[str, dict[str, Any]]:
    """Restrict candidates to those crediting one of target_names. No-op if
    target_names is empty (nothing to filter by)."""
    targets_lower = [t.lower() for t in target_names if t]
    if not targets_lower:
        return merged
    return {vid: c for vid, c in merged.items() if _artist_names_match(c["artists"], targets_lower)}


def _finalize(merged: dict[str, dict[str, Any]], exclude: set[str], limit: int) -> tuple[list[dict[str, Any]], int]:
    """Returns (songs, variants_collapsed) -- the latter is how many
    remix/feature variants were merged into their sibling, for callers that
    want to surface it."""
    merged, collapsed = _collapse_variants(merged)
    ranked = [c for vid, c in merged.items() if vid not in exclude]
    ranked.sort(key=lambda c: (-c["score"], c.get("title") or ""))
    return [
        {
            "videoId": c["videoId"],
            "title": c["title"],
            "artists": c["artists"],
            "album": c["album"],
            "score": c["score"],
            "sources": sorted(c["sources"]),
        }
        for c in ranked[:limit]
    ], collapsed

def same_song(title_a: str | None, artist_a: str | None, title_b: str | None, artist_b: str | None) -> bool:
    """Whether two credits are the same song under different uploads.

    Excluding the seed by videoId isn't enough: YouTube carries the same track
    many times over (topic channels, remasters, "(Official Video)"), so a seed
    resolved by search can come straight back under a different id. Observed
    live -- seeding on "Kryptonite" returned Kryptonite.
    """
    if not title_a or not title_b:
        return False
    if _song_key(title_a) != _song_key(title_b):
        return False
    if not artist_a or not artist_b:
        return True
    a, b = artist_a.lower(), artist_b.lower()
    return a in b or b in a


def _song_key(title: str) -> str:
    """Normalise a title: drop bracketed qualifiers, punctuation and case."""
    lowered = title.lower()
    for opener, closer in (("(", ")"), ("[", "]")):
        while opener in lowered and closer in lowered[lowered.index(opener):]:
            start = lowered.index(opener)
            end = lowered.index(closer, start)
            lowered = (lowered[:start] + " " + lowered[end + 1:]).strip()
    for marker in (" - ", " feat.", " ft.", " with "):
        if marker in lowered:
            lowered = lowered.split(marker)[0]
    return "".join(ch for ch in lowered if ch.isalnum())


def _collapse_variants(pool: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], int]:
    """Collapse remix/feature variants of the same underlying song down to
    one candidate each, keeping the highest-scoring variant.

    "Dead and Gone" and "Dead and Gone (feat. Justin Timberlake)" are both
    legitimate candidates on their own -- different videoIds, sometimes
    different titles or artist credits -- but a recommendation set should
    never hand back both. This runs on the whole candidate pool before
    ranking/truncation/slotting, not after: collapsing post-truncation would
    just leave a gap instead of letting the next-best distinct song in.
    """
    buckets: dict[str, list[str]] = {}
    for vid, c in pool.items():
        buckets.setdefault(_song_key(c.get("title") or "") or vid, []).append(vid)

    collapsed: dict[str, dict[str, Any]] = {}
    dropped = 0
    for vids in buckets.values():
        clusters: list[list[str]] = []
        for vid in vids:
            c = pool[vid]
            artist = (c.get("artists") or [None])[0]
            for cluster in clusters:
                rep = pool[cluster[0]]
                rep_artist = (rep.get("artists") or [None])[0]
                if same_song(c.get("title"), artist, rep.get("title"), rep_artist):
                    cluster.append(vid)
                    break
            else:
                clusters.append([vid])

        for cluster in clusters:
            if len(cluster) == 1:
                collapsed[cluster[0]] = pool[cluster[0]]
                continue
            best = max(cluster, key=lambda v: pool[v].get("score", 0))
            collapsed[best] = pool[best]
            dropped += len(cluster) - 1

    return collapsed, dropped
