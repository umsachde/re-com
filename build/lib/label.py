"""Deciding what mood a song actually has, from whatever evidence exists.

Several layers can each have an opinion about a song (see store.track_mood),
and they differ in cost and trustworthiness. This module is the arbiter: it
picks the best available reading, fills gaps that no layer covered, and knows
how to label a whole library.

Priority order, best first:

  llm         Claude read the lyrics. Handles any language, and irony.
  lyrics      Lyrics were fetched but only mechanically scored.
  atlas       Membership in the *provider's own* editorial mood playlists.
              Cheap, broad, coarse -- and only YouTube Music has one.
  graph_atlas Membership in Deezer playlists found by mood search
              (graph_atlas.py). Works on every backend, which is what makes
              mood portable at all, but ranks below `atlas`: a playlist merely
              titled "sad songs" was named by a stranger, where a YouTube mood
              playlist was filed by the service under a taxonomy.
  artist      Inferred from other songs by the same artist. Free, and it covers
              the catalogue the atlases miss -- which for this library is most
              of the Punjabi, Bollywood and Riddim in it.

A better source always wins outright rather than being averaged in: blending a
confident lyric reading with a one-tag atlas guess makes the good answer worse.
"""

from statistics import mean
from typing import Any, Iterable

import moodspace
import store
import taxonomy

SOURCE_PRIORITY = ("llm", "lyrics", "atlas", "graph_atlas", "artist")
_RANK = {source: index for index, source in enumerate(SOURCE_PRIORITY)}

# Artist propagation is a real signal but a weaker one than direct evidence --
# artists do make quiet songs and loud ones. Discount it, and require more than
# a single labelled track before believing it.
ARTIST_CONFIDENCE_FACTOR = 0.6
ARTIST_MIN_LABELLED = 2

GENRE_PREFIX = "C - "


def _best(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = [r for r in rows if r["source"] in _RANK]
    if not ranked:
        return None
    best = min(ranked, key=lambda r: _RANK[r["source"]])
    return {
        "vector": moodspace.vector(
            valence=best["valence"], energy=best["energy"],
            tension=best["tension"], depth=best["depth"],
        ),
        "source": best["source"],
        "confidence": best["confidence"] if best["confidence"] is not None else 0.5,
    }


def resolve(conn: Any, video_id: str) -> dict[str, Any] | None:
    return _best(store.get_track_moods(conn, video_id))


def resolve_many(conn: Any, video_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Bulk resolve. Ranking touches hundreds of candidates at once, so this
    must be one query rather than one per song."""
    ids = list(dict.fromkeys(v for v in video_ids if v))
    if not ids:
        return {}

    grouped: dict[str, list[dict[str, Any]]] = {}
    chunk = 900  # stay under SQLite's variable limit
    for start in range(0, len(ids), chunk):
        window = ids[start : start + chunk]
        placeholders = ",".join("?" * len(window))
        for row in conn.execute(
            f"SELECT * FROM track_mood WHERE video_id IN ({placeholders})", window
        ):
            grouped.setdefault(row["video_id"], []).append(dict(row))

    resolved = {}
    for video_id, rows in grouped.items():
        best = _best(rows)
        if best:
            resolved[video_id] = best
    return resolved


def _artist_profiles(conn: Any) -> dict[str, tuple[dict[str, float], float]]:
    """Mean mood per lead artist, from directly-evidenced songs only."""
    rows = conn.execute(
        "SELECT t.artists, tm.valence, tm.energy, tm.tension, tm.depth, tm.confidence "
        "FROM track_mood tm JOIN track t USING (video_id) WHERE tm.source != 'artist'"
    ).fetchall()

    grouped: dict[str, list[Any]] = {}
    for row in rows:
        artist = primary_artist(row["artists"])
        if artist:
            grouped.setdefault(artist, []).append(row)

    return {
        artist: (
            moodspace.vector(**{ax: mean(r[ax] for r in group) for ax in moodspace.AXES}),
            mean((r["confidence"] or 0.5) for r in group) * ARTIST_CONFIDENCE_FACTOR,
        )
        for artist, group in grouped.items()
        if len(group) >= ARTIST_MIN_LABELLED
    }


def resolve_or_derive(conn: Any, video_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Resolve moods, deriving one on the spot where none is stored yet.

    Recommendation candidates are mostly songs nobody has labelled -- they came
    out of a radio feed seconds ago. Falling back through raw atlas membership
    and then the artist's own profile is what keeps a mood ranking from only
    ever seeing the small, well-documented corner of the catalogue.
    """
    ids = list(dict.fromkeys(v for v in video_ids if v))
    resolved = resolve_many(conn, ids)

    missing = [v for v in ids if v not in resolved]
    if not missing:
        return resolved

    # Membership that was crawled but never materialised into a vector.
    for video_id in list(missing):
        derived = moodspace.from_atlas_counts(store.atlas_mood_counts(conn, video_id))
        if derived:
            vector, confidence = derived
            store.put_track_mood(conn, video_id, "atlas", vector, confidence)
            resolved[video_id] = {"vector": vector, "source": "atlas", "confidence": confidence}
            missing.remove(video_id)

    if not missing:
        return resolved

    profiles = _artist_profiles(conn)
    if profiles:
        for video_id in missing:
            track = store.get_track(conn, video_id)
            artist = primary_artist((track or {}).get("artists"))
            profile = profiles.get(artist) if artist else None
            if profile:
                vector, confidence = profile
                resolved[video_id] = {"vector": vector, "source": "artist", "confidence": confidence}

    return resolved


def primary_artist(artists: str | None) -> str | None:
    """The lead credit, lowercased -- the grouping key for propagation."""
    if not artists:
        return None
    lead = artists.split(" & ")[0].strip().lower()
    return lead or None


def genre_prior(conn: Any, video_id: str) -> str | None:
    """The user's own genre label for a song, from their hand-curated
    "C - <genre>" playlists. No inference beats a human's own filing."""
    for title in store.library_playlists_for(conn, video_id):
        if title.startswith(GENRE_PREFIX):
            return title[len(GENRE_PREFIX):]
    return None


def propagate_by_artist(conn: Any) -> int:
    """Give unlabelled songs their artist's average mood.

    This is the only gap-filler that needs no API and no credentials, and it
    covers the catalogue the atlas structurally misses. An artist with several
    labelled songs is a decent prior for their unlabelled ones -- discounted,
    because artists do vary.
    """
    direct = {}
    for row in conn.execute(
        "SELECT tm.*, t.artists FROM track_mood tm JOIN track t USING (video_id) "
        f"WHERE tm.source IN ({','.join('?' * (len(SOURCE_PRIORITY) - 1))})",
        [s for s in SOURCE_PRIORITY if s != "artist"],
    ):
        row = dict(row)
        existing = direct.get(row["video_id"])
        if existing is None or _RANK[row["source"]] < _RANK[existing["source"]]:
            direct[row["video_id"]] = row

    by_artist: dict[str, list[dict[str, Any]]] = {}
    for row in direct.values():
        artist = primary_artist(row["artists"])
        if artist:
            by_artist.setdefault(artist, []).append(row)

    profiles = {}
    for artist, rows in by_artist.items():
        if len(rows) < ARTIST_MIN_LABELLED:
            continue
        profiles[artist] = (
            moodspace.vector(**{ax: mean(r[ax] for r in rows) for ax in moodspace.AXES}),
            mean((r["confidence"] or 0.5) for r in rows) * ARTIST_CONFIDENCE_FACTOR,
        )

    if not profiles:
        return 0

    entries = []
    for row in conn.execute(
        "SELECT t.video_id, t.artists FROM track t "
        "WHERE NOT EXISTS (SELECT 1 FROM track_mood tm WHERE tm.video_id = t.video_id "
        "                  AND tm.source != 'artist')"
    ).fetchall():
        artist = primary_artist(row["artists"])
        profile = profiles.get(artist) if artist else None
        if profile:
            entries.append((row["video_id"], profile[0], profile[1]))
    return store.put_track_moods(conn, "artist", entries)


# --- library --------------------------------------------------------------


def sync_library(conn: Any, yt: Any) -> dict[str, int]:
    """Mirror Liked Music and every playlist into the store.

    Keeps the playlist each track came from, because the "C - <genre>"
    playlists are the only genre labels that exist anywhere in this system.
    """
    entries: list[tuple[str, str, bool]] = []
    tracks: list[dict[str, Any]] = []

    liked = yt.get_playlist("LM", limit=None)
    for track in liked.get("tracks", []):
        if track.get("videoId"):
            entries.append((track["videoId"], "Liked Music", True))
            tracks.append(track)

    playlists = 0
    for playlist in yt.get_library_playlists(limit=None):
        playlist_id, title = playlist.get("playlistId"), playlist.get("title")
        if not playlist_id or playlist_id == "LM" or not title:
            continue
        try:
            full = yt.get_playlist(playlist_id, limit=None)
        except Exception:  # noqa: BLE001 - one bad playlist must not stop the sync
            continue
        playlists += 1
        for track in full.get("tracks", []):
            if track.get("videoId"):
                entries.append((track["videoId"], title, False))
                tracks.append(track)

    store.upsert_tracks(conn, tracks)
    rows = store.sync_library(conn, entries)
    return {"playlists": playlists, "rows": rows, "unique_tracks": len(store.library_video_ids(conn))}


def library_coverage_by_language(conn: Any) -> dict[str, dict[str, Any]]:
    """The same coverage number, split by the catalogue it applies to.

    v6's claim was that a neutral atlas closes the non-English gap YouTube's
    editorial moods leave. PLAN.md deliberately refused to attach a number to
    that, because the obvious way to find the non-English subset -- running
    `taxonomy.script_language` over titles -- returns zero here: this library's
    Punjabi and Bollywood titles are romanised ("Brown Munde", not
    "ਬਰਾਊਨ ਮੁੰਡੇ"), so script detection cannot see them.

    `taxonomy.resolve_language` can: it votes with genre-page membership and
    artist labels rather than the characters in a title. Tracks it cannot place
    are reported under `unknown` rather than folded into either side -- an
    unlabelled track is not evidence for or against the claim.
    """
    library = store.library_video_ids(conn)
    if not library:
        return {}

    resolved = resolve_or_derive(conn, library)
    meta = {
        r["video_id"]: dict(r)
        for r in conn.execute(
            "SELECT l.video_id, t.title, t.artists FROM library_track l "
            "LEFT JOIN track t ON t.video_id = l.video_id"
        )
    }

    out: dict[str, dict[str, Any]] = {}
    for video_id in library:
        row = meta.get(video_id) or {}
        language = taxonomy.resolve_language(
            conn, video_id, row.get("title"), row.get("artists")
        )
        key = language["language"] if language else "unknown"
        bucket = out.setdefault(key, {"library": 0, "labelled": 0, "by_source": {}})
        bucket["library"] += 1
        entry = resolved.get(video_id)
        if entry:
            bucket["labelled"] += 1
            bucket["by_source"][entry["source"]] = bucket["by_source"].get(entry["source"], 0) + 1

    for bucket in out.values():
        bucket["coverage"] = round(bucket["labelled"] / bucket["library"], 4)
        bucket["by_source"] = dict(sorted(bucket["by_source"].items(), key=lambda kv: -kv[1]))
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["library"]))


def library_coverage(conn: Any) -> dict[str, Any]:
    """How much of the library actually has a mood, and from where.

    This is the number that decides whether the atlas is a backbone or just a
    prior -- worth reporting honestly rather than hiding behind an average.
    """
    library = store.library_video_ids(conn)
    if not library:
        return {"library": 0, "labelled": 0, "coverage": 0.0, "by_source": {}}

    resolved = resolve_or_derive(conn, library)
    by_source: dict[str, int] = {}
    for entry in resolved.values():
        by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1

    return {
        "library": len(library),
        "labelled": len(resolved),
        "coverage": round(len(resolved) / len(library), 4),
        "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1])),
    }
