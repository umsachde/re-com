"""A mood corpus that belongs to no streaming service.

`atlas.py` builds re-com's mood evidence out of YouTube Music's own "Moods &
moments" playlists. That works, and on YouTube it is richer than anything here
-- but it is unbuildable anywhere else, which is what kept the whole v2 mood
engine YouTube-only. Measured on Spotify (2026-08-23): playlists can be
*found* but not *read* (403 on every one that isn't the user's), and
`audio_features` is gone too, so neither of the two obvious routes to a native
Spotify mood signal exists.

**Deezer allows exactly what Spotify forbids** -- `/search/playlist` finds
playlists and `/playlist/{id}/tracks` reads them, no key and no auth. So the
same idea `atlas.py` proved (learn track <-> mood from playlist membership)
rebuilds on a neutral graph and then works for every backend at once.

Two things are deliberately different from the YouTube crawl:

**There is no editorial mood taxonomy to walk.** YouTube hands you 13 named
moods and their playlists; Deezer has only free-text search. So the moods come
from `moodspace.ANCHORS` -- re-com's own vector space, which is the thing the
counts have to land in anyway -- and each is turned into a set of search
queries. That is a weaker signal per playlist (a playlist merely *called* "sad
songs" is softer evidence than one YouTube files under Sad), which is why
`GRAPH_ATLAS_CONFIDENCE` discounts it below the native atlas.

**The queries are deliberately not English-only.** The YouTube atlas covered
just 4.1% of this library's liked songs, and the misses concentrated on the
Punjabi and Bollywood catalogue its English-centric mood playlists barely
touch. Searching `'punjabi sad'` and `'bollywood romantic'` directly is the
cheapest available fix, and both were verified to return readable playlists.

Moods here are keyed by **Deezer** track id, never by a provider id. A provider
track inherits one through the cached `graph_resolution` bridge
(`propagate_to_provider`), so the labelling work is done once no matter how
many backends are connected.
"""

import time
from typing import Any, Callable, Iterable

import graph
import graph_store
import match
import moodspace
import store

# One page per playlist, matching atlas.py: mood playlists run long, and
# paginating deeper multiplies the crawl for rapidly diminishing evidence.
TRACKS_PER_PLAYLIST = 100

# How many playlists to take per query.
PLAYLISTS_PER_QUERY = 10

# The source name this layer writes under, in both stores.
SOURCE = "graph_atlas"

# Playlist-title search is softer evidence than an editorial mood taxonomy:
# a playlist called "sad songs" was named by a stranger, not filed by the
# service. Discounted accordingly, and below the native atlas in label.py.
GRAPH_ATLAS_CONFIDENCE = 0.75

# Culture/language qualifiers applied to every mood. English is included as the
# bare term. The rest exist because the YouTube atlas reached 4.1% of this
# library and missed almost the entire non-English catalogue.
QUALIFIERS = ("", "hindi", "punjabi", "bollywood", "spanish", "korean", "arabic")

# Search phrasings per mood anchor. Multiple wordings per mood because playlist
# titles are folk language, not a taxonomy -- nobody titles a playlist
# "Energize".
MOOD_QUERIES: dict[str, tuple[str, ...]] = {
    "Sad":       ("sad songs", "heartbreak", "emotional"),
    "Chill":     ("chill", "relaxing", "lofi chill"),
    "Sleep":     ("sleep", "calm sleep", "ambient sleep"),
    "Focus":     ("focus", "study", "concentration"),
    "Commute":   ("driving", "road trip", "commute"),
    "Feel good": ("feel good", "happy", "good vibes"),
    "Romance":   ("romantic", "love songs", "romance"),
    "Energize":  ("energy", "upbeat", "pump up"),
    "Workout":   ("workout", "gym", "running"),
    "Party":     ("party", "dance party", "club"),
    "Gaming":    ("gaming", "gaming mix", "epic gaming"),
}


def queries() -> list[tuple[str, str]]:
    """(mood, query) pairs to crawl, for every mood re-com can place.

    Only moods present in `moodspace.ANCHORS` are produced -- an unplaceable
    mood would be evidence with nowhere to land.
    """
    out: list[tuple[str, str]] = []
    for mood, phrasings in MOOD_QUERIES.items():
        if mood not in moodspace.ANCHORS:
            continue
        for phrasing in phrasings:
            for qualifier in QUALIFIERS:
                query = f"{qualifier} {phrasing}".strip()
                out.append((mood, query))
    return out


_EP_QUERY = "atlas_query"


def crawled_queries(conn: Any) -> set[tuple[str, str]]:
    """(mood, query) pairs already done -- the resume point.

    A crawl of this size will be interrupted; that lesson is already baked into
    `store.atlas_crawl` and applies identically here.

    Read from `graph_fetch` rather than from the playlists a query produced,
    because **a query that found nothing is still done.** Inferring the resume
    point from `graph_playlist` alone silently excluded those: measured on the
    first full crawl, 231 queries ran but only 167 were recorded, so a re-run
    would have paid for 64 known-empty searches again. This is the same
    "asked, nothing there" vs "never asked" distinction `graph_fetch` exists
    for everywhere else.
    """
    rows = conn.execute(
        "SELECT key FROM graph_fetch WHERE endpoint = ?", (_EP_QUERY,)
    ).fetchall()
    out = {tuple(r["key"].split("\t", 1)) for r in rows if "\t" in r["key"]}

    # Rows written before this table was used for queries: fall back to the
    # playlists a query produced so an existing crawl isn't re-run wholesale.
    legacy = conn.execute(
        "SELECT DISTINCT mood, query FROM graph_playlist WHERE status = 'ok' AND query IS NOT NULL"
    ).fetchall()
    return out | {(r["mood"], r["query"]) for r in legacy}


def record_playlist(
    conn: Any,
    playlist_id: int,
    mood: str,
    query: str,
    title: str | None,
    tracks: list[dict[str, Any]],
    status: str = "ok",
) -> int:
    """Store one crawled playlist and check it off in a single transaction --
    a crash mid-playlist must not leave it marked done."""
    rows = [
        (t["id"], mood, playlist_id, t.get("title"), t.get("artist_name"))
        for t in tracks
        if t.get("id")
    ]
    with conn:
        if rows:
            conn.executemany(
                "INSERT INTO graph_playlist_track (track_id, mood, playlist_id, title, artist_name) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(track_id, playlist_id, mood) DO NOTHING",
                rows,
            )
        conn.execute(
            "INSERT INTO graph_playlist (playlist_id, mood, title, query, track_count, status, crawled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(playlist_id, mood) DO UPDATE SET "
            "  title = excluded.title, query = excluded.query, track_count = excluded.track_count, "
            "  status = excluded.status, crawled_at = excluded.crawled_at",
            (playlist_id, mood, title, query, len(rows), status, time.time()),
        )
    return len(rows)


def crawl(
    conn: Any,
    *,
    resume: bool = True,
    limit: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Search Deezer for mood playlists and read their tracks.

    Resumable by (mood, query). A query that returns nothing is still recorded
    as attempted, so a re-run doesn't pay for it again.
    """
    stats = {"queries": 0, "playlists": 0, "tracks": 0, "skipped": 0}
    done = crawled_queries(conn) if resume else set()

    pending = [pair for pair in queries() if pair not in done]
    stats["skipped"] = len(queries()) - len(pending)
    if limit is not None:
        pending = pending[:limit]

    for mood, query in pending:
        stats["queries"] += 1
        found = 0
        for playlist in graph.search_playlists(query, limit=PLAYLISTS_PER_QUERY, sleep=sleep):
            tracks = graph.playlist_tracks(playlist["id"], limit=TRACKS_PER_PLAYLIST, sleep=sleep)
            added = record_playlist(
                conn, playlist["id"], mood, query, playlist.get("title"), tracks
            )
            stats["playlists"] += 1
            stats["tracks"] += added
            found += 1
        # Checked off whether or not it found anything -- see crawled_queries.
        graph_store.record_fetch(
            conn, _EP_QUERY, f"{mood}\t{query}", graph_store.STATUS_OK, found
        )
        if on_progress:
            on_progress({**stats, "mood": mood, "query": query})
    return stats


def materialize_moods(conn: Any) -> int:
    """Collapse playlist membership into one mood vector per Deezer track.

    A separate pass from the crawl, exactly as in `atlas.py`, so the anchor
    table can be retuned and re-applied in seconds without re-crawling.
    """
    rows = conn.execute(
        "SELECT track_id, mood, COUNT(DISTINCT playlist_id) AS n "
        "FROM graph_playlist_track GROUP BY track_id, mood"
    ).fetchall()

    counts: dict[int, dict[str, int]] = {}
    for row in rows:
        counts.setdefault(row["track_id"], {})[row["mood"]] = row["n"]

    written = 0
    now = time.time()
    with conn:
        for track_id, mood_counts in counts.items():
            result = moodspace.from_atlas_counts(mood_counts)
            if result is None:
                continue
            vector, confidence = result
            conn.execute(
                "INSERT INTO graph_track_mood "
                "(track_id, source, valence, energy, tension, depth, confidence, labeled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(track_id, source) DO UPDATE SET "
                "  valence = excluded.valence, energy = excluded.energy, "
                "  tension = excluded.tension, depth = excluded.depth, "
                "  confidence = excluded.confidence, labeled_at = excluded.labeled_at",
                (
                    track_id, SOURCE, vector["valence"], vector["energy"],
                    vector["tension"], vector["depth"],
                    confidence * GRAPH_ATLAS_CONFIDENCE, now,
                ),
            )
            written += 1
    return written


def get_mood(conn: Any, track_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT valence, energy, tension, depth, confidence FROM graph_track_mood "
        "WHERE track_id = ? AND source = ?",
        (track_id, SOURCE),
    ).fetchone()
    return dict(row) if row else None


def propagate_to_provider(
    store_conn: Any,
    graph_conn: Any,
    tracks: Iterable[dict[str, Any]],
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Give provider tracks the graph's mood, via the cached id bridge.

    Writes into the provider-scoped `store.track_mood` under `SOURCE`, so
    `label.py` and everything downstream (`pick_seeds`, `atlas_neighbours`,
    `recommend_for_mood`) keep reading exactly one table and need no knowledge
    of the graph at all.

    `tracks` are store rows: {"video_id", "title", "artists"}.
    """
    stats = {"labeled": 0, "unresolved": 0, "no_mood": 0}
    entries = []

    for row in tracks:
        title = row.get("title")
        if not title:
            stats["unresolved"] += 1
            continue
        artist = row.get("artists")
        primary = artist.split(" & ")[0] if isinstance(artist, str) and artist else artist

        resolved = graph.resolve(graph_conn, title, primary, sleep=sleep)
        if not resolved:
            stats["unresolved"] += 1
            continue

        mood = get_mood(graph_conn, resolved["id"])
        if not mood:
            stats["no_mood"] += 1
            continue

        entries.append((
            row["video_id"],
            moodspace.vector(
                valence=mood["valence"], energy=mood["energy"],
                tension=mood["tension"], depth=mood["depth"],
            ),
            mood["confidence"],
        ))
        stats["labeled"] += 1
        if on_progress:
            on_progress({**stats, "title": title})

    if entries:
        store.put_track_moods(store_conn, SOURCE, entries)
    return stats


def coverage(conn: Any) -> dict[str, Any]:
    """What the graph atlas actually holds, so gaps stay visible."""
    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "queries_crawled": len(crawled_queries(conn)),
        "queries_total": len(queries()),
        "playlists": count("SELECT COUNT(DISTINCT playlist_id) FROM graph_playlist"),
        "tracks": count("SELECT COUNT(DISTINCT track_id) FROM graph_playlist_track"),
        "memberships": count("SELECT COUNT(*) FROM graph_playlist_track"),
        "moods_materialized": count(
            f"SELECT COUNT(*) FROM graph_track_mood WHERE source = '{SOURCE}'"
        ),
    }
