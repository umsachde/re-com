"""SQLite cache for the neutral music graph -- deliberately NOT per-provider.

`store.py` is scoped per backend (`store.db`, `store-spotify.db`) because every
id in it belongs to one provider's namespace and mixing them is silently wrong:
a YouTube videoId is 11 chars, a Spotify track id is 22, and sharing one file
made the Spotify instance read a 1,499-entry exclusion set that could never
match anything.

**The graph is the opposite case, and it needs the opposite treatment.** Deezer
ids are service-neutral. "Excuses -- AP Dhillon resolves to Deezer track
2679347" is equally true for the YouTube instance and the Spotify one, and so
is "AP Dhillon's related artists are Diljit Dosanjh, Shubh, Garry Sandhu".
Scoping this file per provider would resolve every artist twice today and grow
a third copy on the next backend, for no correctness benefit whatsoever.

So this is one unscoped `~/.recom/graph.db` shared by every provider instance,
and it is what keeps v6's provider-neutral/provider-scoped split honest at the
file level. PLAN.md's deferred alternative -- adding a `provider` column across
~8 tables in `store.py` -- is still not needed: a second database file is the
cheaper correct shape, and this one wants the opposite scoping anyway.

**Negative results are cached like positive ones.** That is `tempo.py`'s
hard-won lesson: Deezer genuinely has no BPM for much of the non-English
catalogue, and rediscovering that costs two requests per song per pass. The
same is true of an artist with no related artists, or a title that resolves to
nothing -- `graph_fetch` records that an endpoint was tried and came back
empty, so "never asked" stays distinguishable from "asked, nothing there".
"""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

# Unscoped on purpose -- see the module docstring. `provider.scoped_path` is
# deliberately NOT applied here.
DEFAULT_DB_PATH = Path.home() / ".recom" / "graph.db"
DB_PATH = Path(os.environ.get("RECOM_GRAPH_DB_PATH") or DEFAULT_DB_PATH)

# Resolution outcomes, mirroring tempo.py's vocabulary.
STATUS_OK = "ok"
STATUS_NO_MATCH = "no_match"

SCHEMA = """
-- Title+artist text -> Deezer identity. The bridge every graph signal crosses.
-- Keyed on the *normalised* keys (match.song_key / lowercased artist) rather
-- than raw text so "Excuses (Official Video)" and "Excuses" share one row.
CREATE TABLE IF NOT EXISTS graph_resolution (
    song_key    TEXT NOT NULL,
    artist_key  TEXT NOT NULL,
    track_id    INTEGER,
    artist_id   INTEGER,
    title       TEXT,
    artist_name TEXT,
    status      TEXT,
    resolved_at REAL,
    PRIMARY KEY (song_key, artist_key)
);
CREATE INDEX IF NOT EXISTS idx_graph_resolution_track ON graph_resolution (track_id);

-- Artist adjacency: the signal that replaces the per-track radio Deezer has no
-- equivalent for. Measured culturally correct on this library's Punjabi
-- catalogue, which is the thing that actually matters here.
CREATE TABLE IF NOT EXISTS graph_artist_related (
    artist_id  INTEGER NOT NULL,
    related_id INTEGER NOT NULL,
    name       TEXT,
    position   INTEGER,
    PRIMARY KEY (artist_id, related_id)
);

-- An artist's tracks. `kind` separates /top (stable, ranked) from /radio
-- (best-effort, uneven) so a caller can weight them differently rather than
-- discovering mid-ranking that they are not the same quality of evidence.
CREATE TABLE IF NOT EXISTS graph_artist_track (
    artist_id   INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    track_id    INTEGER NOT NULL,
    title       TEXT,
    artist_name TEXT,
    track_artist_id INTEGER,
    position    INTEGER,
    PRIMARY KEY (artist_id, kind, track_id)
);

-- "We asked this endpoint for this key, and here is when and how it went."
-- Without it an artist with genuinely zero related artists is indistinguishable
-- from one never looked up, and gets re-fetched forever.
CREATE TABLE IF NOT EXISTS graph_fetch (
    endpoint   TEXT NOT NULL,
    key        TEXT NOT NULL,
    status     TEXT,
    count      INTEGER,
    fetched_at REAL,
    PRIMARY KEY (endpoint, key)
);

-- The provider-neutral mood atlas. Deezer allows exactly what Spotify forbids:
-- searching playlists AND reading their tracks. Same shape as store.py's
-- atlas_crawl/atlas_membership, because it is the same idea on a neutral graph.
CREATE TABLE IF NOT EXISTS graph_playlist (
    playlist_id INTEGER NOT NULL,
    mood        TEXT NOT NULL,
    title       TEXT,
    query       TEXT,
    track_count INTEGER,
    status      TEXT,
    crawled_at  REAL,
    PRIMARY KEY (playlist_id, mood)
);

CREATE TABLE IF NOT EXISTS graph_playlist_track (
    track_id    INTEGER NOT NULL,
    mood        TEXT NOT NULL,
    playlist_id INTEGER NOT NULL,
    title       TEXT,
    artist_name TEXT,
    PRIMARY KEY (track_id, playlist_id, mood)
);
CREATE INDEX IF NOT EXISTS idx_graph_pltrack_track ON graph_playlist_track (track_id);
CREATE INDEX IF NOT EXISTS idx_graph_pltrack_mood  ON graph_playlist_track (mood);

-- Mood vectors keyed by DEEZER id, never by provider id. A provider track
-- inherits one by resolving through graph_resolution, so the same labelling
-- work is never repeated per backend.
CREATE TABLE IF NOT EXISTS graph_track_mood (
    track_id   INTEGER NOT NULL,
    source     TEXT NOT NULL,
    valence    REAL,
    energy     REAL,
    tension    REAL,
    depth      REAL,
    confidence REAL,
    labeled_at REAL,
    PRIMARY KEY (track_id, source)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the graph cache.

    Same PRAGMA setup as `store.connect`, for the same reason: a long
    background crawl (`scripts/build_graph_atlas.py`) runs while live tool
    calls read, so WAL and a busy timeout are not optional.
    """
    target = Path(path) if path is not None else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn


# --- fetch bookkeeping ------------------------------------------------------


def record_fetch(conn: sqlite3.Connection, endpoint: str, key: Any, status: str, count: int = 0) -> None:
    conn.execute(
        "INSERT INTO graph_fetch (endpoint, key, status, count, fetched_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(endpoint, key) DO UPDATE SET "
        "status = excluded.status, count = excluded.count, fetched_at = excluded.fetched_at",
        (endpoint, str(key), status, count, time.time()),
    )
    conn.commit()


def was_fetched(conn: sqlite3.Connection, endpoint: str, key: Any) -> bool:
    """Whether this endpoint/key was ever attempted.

    The point of the table: an empty cached result must not look identical to
    a cache miss, or every zero-result lookup re-hits the network forever.
    """
    row = conn.execute(
        "SELECT 1 FROM graph_fetch WHERE endpoint = ? AND key = ?", (endpoint, str(key))
    ).fetchone()
    return row is not None


# --- resolution -------------------------------------------------------------


def get_resolution(conn: sqlite3.Connection, song_key: str, artist_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM graph_resolution WHERE song_key = ? AND artist_key = ?",
        (song_key, artist_key),
    ).fetchone()
    return dict(row) if row else None


def put_resolution(
    conn: sqlite3.Connection,
    song_key: str,
    artist_key: str,
    *,
    track_id: int | None,
    artist_id: int | None,
    title: str | None,
    artist_name: str | None,
    status: str,
) -> None:
    conn.execute(
        "INSERT INTO graph_resolution "
        "(song_key, artist_key, track_id, artist_id, title, artist_name, status, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(song_key, artist_key) DO UPDATE SET "
        "track_id = excluded.track_id, artist_id = excluded.artist_id, title = excluded.title, "
        "artist_name = excluded.artist_name, status = excluded.status, resolved_at = excluded.resolved_at",
        (song_key, artist_key, track_id, artist_id, title, artist_name, status, time.time()),
    )
    conn.commit()


# --- artist adjacency -------------------------------------------------------


def put_related_artists(conn: sqlite3.Connection, artist_id: int, related: Iterable[dict[str, Any]]) -> int:
    rows = [
        (artist_id, r["id"], r.get("name"), position)
        for position, r in enumerate(related)
        if r.get("id")
    ]
    conn.executemany(
        "INSERT INTO graph_artist_related (artist_id, related_id, name, position) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(artist_id, related_id) DO UPDATE SET "
        "name = excluded.name, position = excluded.position",
        rows,
    )
    conn.commit()
    return len(rows)


def get_related_artists(conn: sqlite3.Connection, artist_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT related_id AS id, name FROM graph_artist_related WHERE artist_id = ? ORDER BY position",
        (artist_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def put_artist_tracks(conn: sqlite3.Connection, artist_id: int, kind: str, tracks: Iterable[dict[str, Any]]) -> int:
    rows = [
        (artist_id, kind, t["id"], t.get("title"), t.get("artist_name"), t.get("artist_id"), position)
        for position, t in enumerate(tracks)
        if t.get("id")
    ]
    conn.executemany(
        "INSERT INTO graph_artist_track "
        "(artist_id, kind, track_id, title, artist_name, track_artist_id, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(artist_id, kind, track_id) DO UPDATE SET "
        "title = excluded.title, artist_name = excluded.artist_name, "
        "track_artist_id = excluded.track_artist_id, position = excluded.position",
        rows,
    )
    conn.commit()
    return len(rows)


def get_artist_tracks(conn: sqlite3.Connection, artist_id: int, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT track_id AS id, title, artist_name, track_artist_id AS artist_id "
        "FROM graph_artist_track WHERE artist_id = ? AND kind = ? ORDER BY position",
        (artist_id, kind),
    ).fetchall()
    return [dict(r) for r in rows]


# --- stats ------------------------------------------------------------------


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the graph actually holds. Feeds index_status()/quality_check so
    coverage gaps stay visible instead of silent."""

    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "resolved_tracks": count(
            f"SELECT COUNT(*) FROM graph_resolution WHERE status = '{STATUS_OK}'"
        ),
        "unresolved_tracks": count(
            f"SELECT COUNT(*) FROM graph_resolution WHERE status != '{STATUS_OK}'"
        ),
        "artists_with_related": count("SELECT COUNT(DISTINCT artist_id) FROM graph_artist_related"),
        "artist_tracks": count("SELECT COUNT(*) FROM graph_artist_track"),
        "playlists": count("SELECT COUNT(DISTINCT playlist_id) FROM graph_playlist"),
        "playlist_tracks": count("SELECT COUNT(DISTINCT track_id) FROM graph_playlist_track"),
        "moods_labeled": count("SELECT COUNT(DISTINCT track_id) FROM graph_track_mood"),
    }
