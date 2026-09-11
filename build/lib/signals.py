"""Candidate generation: the multi-signal discovery core.

Extracted from server.py so that both v1's similarity tools and v2's mood
recommendations draw candidates the same way. The ranking on top differs; the
way songs are found does not.

A candidate's score is how many distinct (seed, signal) pairs surfaced it,
which makes agreement between independent signals the thing that ranks, rather
than trust in any one algorithm. That idea is unchanged since v1 and is why
adding and removing signals is cheap: more agreeing sources rank higher, fewer
available sources degrade quality instead of breaking the request.

**v6 made the set of signals variable rather than fixed.** v1 hard-coded three
YouTube signals -- radio, the separate "related" feed, and artist expansion --
because there was one backend. Measured against Spotify (2026-08-23), two of
those three are unbuildable: `/recommendations` 404s and `artist_related_artists`
/`artist_top_tracks` 403 under the post-Nov-2024 restriction, and
`recommend_from_song` returned zero songs there. So signals are now declared and
gated:

  native   radio / related / artist   -- gated on `provider.capabilities()`
  graph    graph_artist / graph_radio / graph_related -- always available

The graph signals come from Deezer (`graph.py`), belong to no provider, and
cannot be revoked by one. They are artist-centric because Deezer has no
track-level radio, which is a real quality trade on YouTube -- so native
signals are *added to*, never replaced. A backend with fewer native signals
simply has fewer sources agreeing.

**Graph candidates arrive with no provider id.** They are keyed by
`graph:<deezer id>` through ranking and resolved back to provider ids lazily by
`resolve_candidates`, because resolving a 500-candidate pool eagerly would cost
one provider search each. That makes exclusion two-stage -- see `_finalize`.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import match
import provider as _provider
from provider import ProviderError

_SOURCE_RADIO = "radio"
_SOURCE_RELATED = "related"
_SOURCE_ARTIST = "artist"
_RELATED_ARTISTS_TO_EXPAND = 2  # how many of the seed artist's related artists to also pull top songs from

# Graph-sourced candidates are keyed by this prefix + their Deezer track id,
# because they have no provider id until resolve_candidates runs.
GRAPH_KEY_PREFIX = "graph:"

# How many of the seed artist's graph neighbours to expand, and how many tracks
# to take per artist. Deliberately wider than _RELATED_ARTISTS_TO_EXPAND: with
# no track-level radio available, adjacency is the only reach the graph has.
_GRAPH_RELATED_TO_EXPAND = 3
_GRAPH_TRACKS_PER_ARTIST = 10

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


def seed_metadata(yt: Any, seed_video_id: str, watch: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Title/artist for the seed, as cheaply as this backend allows.

    The graph needs it on every request to resolve the seed onto Deezer. Three
    sources, cheapest first: a radio response already in hand, the provider's
    optional `get_track_meta`, then `get_watch_playlist` as the universal
    fallback (every Provider implements it, but on Spotify it also pays for a
    restricted `/recommendations` call, which is why `get_track_meta` exists).
    """
    if watch:
        for track in watch.get("tracks", []):
            if track.get("videoId") == seed_video_id and track.get("title"):
                return _norm_track(track)

    getter = getattr(yt, "get_track_meta", None)
    if getter is not None:
        try:
            meta = getter(seed_video_id)
        except _SIGNAL_ERRORS:
            meta = None
        if meta and meta.get("title"):
            return _norm_track(meta)

    try:
        fetched = yt.get_watch_playlist(videoId=seed_video_id, limit=1, radio=False)
    except _SIGNAL_ERRORS:
        return None
    for track in (fetched or {}).get("tracks", []):
        if track.get("videoId") == seed_video_id and track.get("title"):
            return _norm_track(track)
    return None


def _gather_seed_candidates(
    yt: Any,
    seed_video_id: str,
    seed_artist_names: list[str] | None = None,
    *,
    graph_conn: Any = None,
    seed_meta: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Pull every available signal's candidates for one seed song.

    Returns key -> {normalized track fields..., "sources": set[str]}, where key
    is a provider id for native candidates and `graph:<deezer id>` for graph
    ones. A signal that fails is skipped rather than aborting the whole seed,
    and a signal the backend cannot supply is never attempted.

    `graph_conn` (a `graph_store` connection) enables the graph signals; pass
    None to get v1's native-only behaviour exactly.

    If `seed_artist_names` is passed, the seed track's own artist name(s) are
    appended to it as a side effect -- lets callers recover the seed's artist
    without a second lookup, e.g. to filter candidates down to that artist.
    """
    found: dict[str, dict[str, Any]] = {}
    caps = _provider.capabilities_of(yt)

    def add(item: dict[str, Any], source: str) -> None:
        vid = item.get("videoId")
        if not vid or vid == seed_video_id:
            return
        if vid not in found:
            found[vid] = {**_norm_track(item), "sources": set()}
        found[vid]["sources"].add(source)

    watch = None
    if _provider.CAP_RADIO in caps:
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
        if related_browse_id and _provider.CAP_RELATED in caps:
            try:
                sections = yt.get_song_related(related_browse_id)
            except _SIGNAL_ERRORS:
                sections = []
            for section in sections:
                for item in section.get("contents") or []:
                    if isinstance(item, dict) and item.get("videoId"):
                        add(item, _SOURCE_RELATED)

    if seed_artist_id and _provider.CAP_ARTIST in caps:
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

    if graph_conn is not None:
        meta = seed_meta or seed_metadata(yt, seed_video_id, watch)
        if meta and seed_artist_names is not None and not seed_artist_names:
            seed_artist_names.extend(meta.get("artists") or [])
        _add_graph_candidates(found, graph_conn, meta)

    return found


def _add_graph_candidates(
    found: dict[str, dict[str, Any]], graph_conn: Any, meta: dict[str, Any] | None
) -> None:
    """Merge Deezer graph neighbours into the candidate pool.

    Imported lazily so that `signals` stays importable (and every existing test
    keeps running) without the graph modules being wired up.

    Graph candidates carry `graphRef` and a null `videoId`: they are real
    songs with no provider identity yet. Nothing downstream may treat them as
    provider ids -- `_finalize` excludes them by title/artist and
    `resolve_candidates` gives them ids afterwards.
    """
    if not meta or not meta.get("title"):
        return

    import graph  # local: keeps the graph optional at import time
    import graph_store

    # This runs on a worker thread when there is more than one seed, and a
    # sqlite connection belongs to the thread that opened it. See
    # graph_store.for_thread.
    graph_conn = graph_store.for_thread(graph_conn)

    artists = meta.get("artists") or []
    primary = artists[0] if artists else None

    seed = graph.resolve(graph_conn, meta["title"], primary)
    if not seed:
        return

    for row in graph.neighbours(
        graph_conn,
        seed,
        related_to_expand=_GRAPH_RELATED_TO_EXPAND,
        per_artist=_GRAPH_TRACKS_PER_ARTIST,
    ):
        key = f"{GRAPH_KEY_PREFIX}{row['id']}"
        if key not in found:
            found[key] = {
                "videoId": None,
                "graphRef": {"trackId": row["id"], "artistId": row.get("artist_id")},
                "title": row.get("title"),
                "artists": [row["artist_name"]] if row.get("artist_name") else [],
                "album": None,
                "sources": set(),
            }
        found[key]["sources"].add(row["source"])


def gather_seeds(
    yt: Any,
    seed_video_ids: Any,
    *,
    skip_failures: bool = True,
    max_workers: int | None = None,
    graph_conn: Any = None,
    seed_meta: dict[str, dict[str, Any]] | None = None,
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

    meta = seed_meta or {}

    def gather(vid: str) -> dict[str, dict[str, Any]]:
        return _gather_seed_candidates(
            yt, vid, graph_conn=graph_conn, seed_meta=meta.get(vid)
        )

    # One seed doesn't need a pool, and staying on this thread keeps the
    # single-seed path (recommend_from_song) exactly as it was.
    if len(ids) == 1:
        try:
            return [gather(ids[0])]
        except Exception:  # noqa: BLE001 - honour skip_failures uniformly
            if not skip_failures:
                raise
            return []

    workers = max_workers or min(len(ids), max(1, SEED_WORKERS))
    out: list[dict[str, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recom-seed") as pool:
        futures = [pool.submit(gather, vid) for vid in ids]
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
                # Carried, not scored: a graph candidate needs its Deezer
                # reference to survive ranking so resolve_candidates can give
                # it a provider id afterwards. The scoring rule itself is
                # untouched -- agreement between distinct (seed, source) pairs.
                if data.get("graphRef"):
                    entry["graphRef"] = data["graphRef"]
                merged[vid] = entry
            entry["sources"] |= data["sources"]
            entry["score"] += len(data["sources"])
    return merged


_artist_names_match = match.artist_names_match


def _filter_same_artist(merged: dict[str, dict[str, Any]], target_names: list[str]) -> dict[str, dict[str, Any]]:
    """Restrict candidates to those crediting one of target_names. No-op if
    target_names is empty (nothing to filter by)."""
    targets_lower = [t.lower() for t in target_names if t]
    if not targets_lower:
        return merged
    return {vid: c for vid, c in merged.items() if _artist_names_match(c["artists"], targets_lower)}


def _finalize(
    merged: dict[str, dict[str, Any]],
    exclude: set[str],
    limit: int,
    exclude_index: dict[str, list[str | None]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Returns (songs, variants_collapsed) -- the latter is how many
    remix/feature variants were merged into their sibling, for callers that
    want to surface it.

    **Exclusion is two-stage, and stage one happens here.** `exclude` is a set
    of provider ids, which graph candidates do not have yet, so filtering on it
    alone would silently stop applying the never-recommend-a-library-song
    guarantee to every graph-sourced candidate -- the single worst failure this
    project could have. `exclude_index` (a `match.build_index` of the library's
    titles/artists) catches them on text instead, before resolution.

    Stage two runs in `resolve_candidates`, by provider id, once they have one.
    """
    merged, collapsed = _collapse_variants(merged)

    ranked = []
    for key, c in merged.items():
        if c.get("videoId"):
            if c["videoId"] in exclude:
                continue
        elif exclude_index is not None and match.matches_any(
            c.get("title"), (c.get("artists") or [None])[0], exclude_index
        ):
            continue
        ranked.append(c)

    ranked.sort(key=lambda c: (-c["score"], c.get("title") or ""))
    out = []
    for c in ranked[:limit]:
        song = {
            "videoId": c["videoId"],
            "title": c["title"],
            "artists": c["artists"],
            "album": c["album"],
            "score": c["score"],
            "sources": sorted(c["sources"]),
        }
        if c.get("graphRef"):
            song["graphRef"] = c["graphRef"]
        out.append(song)
    return out, collapsed

# Song identity lives in match.py -- v6 needs the same rules to compare a
# Deezer credit against a provider credit, and PLAN.md warned that would become
# a third copy of this logic. Kept as module-level names here because they are
# part of signals' surface for callers and tests.
same_song = match.same_song
_song_key = match.song_key


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


# --- lazy resolution --------------------------------------------------------


def _resolve_one(yt: Any, song: dict[str, Any]) -> str | None:
    """Find this graph candidate's provider id, or None.

    A graph candidate is a real song known by title and artist; the provider
    knows the same song under its own id. One search bridges them, gated by
    `match.same_song` so a near-miss is dropped rather than substituted.
    """
    title = song.get("title")
    if not title:
        return None
    artists = song.get("artists") or []
    primary = artists[0] if artists else None
    query = f"{title} {primary}".strip() if primary else title

    try:
        hits = yt.search(query, filter="songs", limit=5)
    except _SIGNAL_ERRORS:
        return None

    for hit in hits or []:
        if not hit.get("videoId"):
            continue
        norm = _norm_track(hit)
        hit_artist = (norm.get("artists") or [None])[0]
        if match.same_song(title, primary, norm.get("title"), hit_artist):
            return hit["videoId"]
    return None


def resolve_candidates(
    yt: Any,
    songs: list[dict[str, Any]],
    limit: int,
    exclude: set[str] | None = None,
    *,
    max_workers: int | None = None,
    max_resolve: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Give graph candidates provider ids, lazily. Returns (songs, dropped).

    **Why lazily.** The graph returns "Diljit Dosanjh -- Born to Shine"; the
    provider needs its own id to return it, at roughly one search per
    candidate. Ranking happens on graph-side metadata first so only the top of
    the pool is ever resolved -- resolving a 500-candidate pool eagerly would
    be absurd.

    **This is stage two of exclusion.** A graph candidate cleared the
    title/artist index in `_finalize`; now that it has a real provider id it is
    checked against the authoritative id set, which catches anything fuzzy
    matching missed. Because that can drop candidates, callers pass a pool
    larger than `limit` (see `RESOLVE_BUFFER`) and take what survives.

    A candidate the provider cannot find is dropped and counted, never
    substituted -- the same partial-results philosophy as a failed signal.
    Native candidates already have ids and pass through untouched.

    `max_resolve` caps how many provider searches this may perform. The
    language and tempo filters need a pool ~12x the requested limit, because
    they drop a great deal -- fine when candidates are free, but every graph
    candidate in that pool costs a round trip. Native candidates still fill the
    deep pool; only the searching is bounded. Graph candidates past the cap are
    left unresolved and drop out, which the caller reports as a note.
    """
    exclude = exclude or set()
    out: list[dict[str, Any]] = []
    dropped = 0
    index = 0
    budget = len(songs) if max_resolve is None else max_resolve

    while index < len(songs) and len(out) < limit:
        # Walk in rank order and only resolve as far down the pool as we
        # actually need. Measured on YouTube, where native signals fill the
        # whole response and every graph candidate below them is dead weight:
        # resolving the entire pool eagerly took a 5.0s request to 18.5s for an
        # identical top ten. Native candidates already have ids and cost
        # nothing, so a fully-native response now does zero searches.
        window = songs[index:index + max(1, limit - len(out))]
        index += len(window)

        pending = [s for s in window if not s.get("videoId")][:max(0, budget)]
        if pending:
            budget -= len(pending)
            workers = max_workers or min(len(pending), max(1, SEED_WORKERS))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recom-resolve") as pool:
                for song, resolved in zip(pending, pool.map(lambda s: _resolve_one(yt, s), pending)):
                    song["videoId"] = resolved

        for song in window:
            vid = song.get("videoId")
            if not vid or vid in exclude:
                dropped += 1
                continue
            song.pop("graphRef", None)
            out.append(song)
            if len(out) >= limit:
                break

    return out, dropped


# How much bigger than `limit` a pool should be before resolution, so that
# candidates lost to stage-two exclusion or an unfindable provider id don't
# make the response come back short.
RESOLVE_BUFFER = 1.6


def resolve_pool_size(limit: int) -> int:
    return max(limit + 5, int(limit * RESOLVE_BUFFER))
