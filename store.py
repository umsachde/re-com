"""SQLite persistence for re-com v2.

v1 needed no storage -- every tool call rebuilt what it needed from the API.
v2 can't work that way: the mood atlas is ~180k rows crawled over half an hour,
and mood labels are expensive enough that they must be computed once and kept.

Layout note: PLAN_V2.md sketches a nested `recom/` package. This project
is currently flat (server.py at the root), so v2 modules stay flat too rather
than mixing conventions mid-build. The v1 library-exclusion cache deliberately
stays as its own JSON file (see server.py) -- it works, it's tested, and
rewriting it into SQLite purely for tidiness isn't worth the churn yet.
"""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

_DEFAULT_DB_PATH = Path.home() / ".recom" / "store.db"
DB_PATH = Path(os.environ.get("RECOM_DB_PATH") or _DEFAULT_DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS track (
    video_id TEXT PRIMARY KEY,
    title    TEXT,
    artists  TEXT,
    album    TEXT
);

-- One row per (song, mood playlist it appears in). The raw evidence behind
-- every atlas-derived mood vector; kept rather than collapsed so a changed
-- anchor table can be re-applied without re-crawling.
CREATE TABLE IF NOT EXISTS atlas_membership (
    video_id       TEXT NOT NULL,
    mood           TEXT NOT NULL,
    playlist_id    TEXT NOT NULL,
    playlist_title TEXT,
    PRIMARY KEY (video_id, playlist_id, mood)
);
CREATE INDEX IF NOT EXISTS idx_atlas_video ON atlas_membership (video_id);
CREATE INDEX IF NOT EXISTS idx_atlas_mood  ON atlas_membership (mood);

-- Crawl checkpoint. A 2,200-playlist crawl WILL be interrupted -- rate limits,
-- expired auth, a closed laptop -- so every playlist is recorded as it lands
-- and the crawler resumes from here instead of starting over.
CREATE TABLE IF NOT EXISTS atlas_crawl (
    playlist_id TEXT NOT NULL,
    mood        TEXT NOT NULL,
    title       TEXT,
    track_count INTEGER,
    status      TEXT,
    crawled_at  REAL,
    PRIMARY KEY (playlist_id, mood)
);

-- Mood vectors, one row per (song, source). Sources are kept separate rather
-- than merged so a cheap atlas guess never silently overwrites a lyric-derived
-- reading, and so re-labelling one layer doesn't destroy the others.
CREATE TABLE IF NOT EXISTS track_mood (
    video_id   TEXT NOT NULL,
    source     TEXT NOT NULL,
    valence    REAL,
    energy     REAL,
    tension    REAL,
    depth      REAL,
    confidence REAL,
    labeled_at REAL,
    PRIMARY KEY (video_id, source)
);
CREATE INDEX IF NOT EXISTS idx_track_mood_video ON track_mood (video_id);

-- Lyrics are the deepest mood signal available, and cost 2 API calls each, so
-- they are fetched once and kept. `available = 0` records a definitive "this
-- track has no lyrics" so we never pay to re-discover that.
CREATE TABLE IF NOT EXISTS lyrics (
    video_id   TEXT PRIMARY KEY,
    text       TEXT,
    source     TEXT,
    available  INTEGER,
    fetched_at REAL
);

-- The user's own library, with the playlist each track came from. The
-- hand-curated "C - <genre>" playlists are the only genre labels available
-- anywhere, and they are better than anything we could infer.
CREATE TABLE IF NOT EXISTS library_track (
    video_id       TEXT NOT NULL,
    playlist_title TEXT NOT NULL,
    is_liked       INTEGER,
    synced_at      REAL,
    PRIMARY KEY (video_id, playlist_title)
);
CREATE INDEX IF NOT EXISTS idx_library_video ON library_track (video_id);

-- get_history() reports only "Today"/"Yesterday" -- order, never a clock time.
-- Snapshotting it on a schedule is the only way to build a real timeline.
CREATE TABLE IF NOT EXISTS history_log (
    video_id    TEXT NOT NULL,
    observed_at REAL NOT NULL,
    position    INTEGER,
    bucket      TEXT,
    PRIMARY KEY (video_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_history_time ON history_log (observed_at);

-- What we served, and under what mood. Without this there is nothing to learn
-- from later.
CREATE TABLE IF NOT EXISTS recommendation (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    served_at REAL,
    feeling  TEXT,
    arc      TEXT,
    slot     INTEGER,
    score    REAL,
    valence  REAL,
    energy   REAL,
    tension  REAL,
    depth    REAL
);
CREATE INDEX IF NOT EXISTS idx_rec_video ON recommendation (video_id);

CREATE TABLE IF NOT EXISTS feedback (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    reaction TEXT NOT NULL,
    source   TEXT,
    at       REAL
);

-- Tempo from Deezer. Negative results are recorded too: Deezer genuinely has
-- no BPM for much of the non-English catalogue, and rediscovering that costs
-- two requests per song per pass. Deliberately NOT propagated by artist --
-- an artist's songs share a sensibility, not a tempo.
CREATE TABLE IF NOT EXISTS track_tempo (
    video_id    TEXT PRIMARY KEY,
    bpm         REAL,
    status      TEXT,
    deezer_id   INTEGER,
    resolved_at REAL
);

-- Genre labels harvested from YouTube's genre-category pages. Feeds language
-- inference; see taxonomy.py.
CREATE TABLE IF NOT EXISTS genre_membership (
    video_id TEXT NOT NULL,
    genre    TEXT NOT NULL,
    PRIMARY KEY (video_id, genre)
);
CREATE INDEX IF NOT EXISTS idx_genre_video ON genre_membership (video_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the store and ensure the schema exists."""
    target = Path(path) if path is not None else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL lets a reader (a tool call) work while a writer (the atlas crawler)
    # runs; busy_timeout makes the loser of a write race wait rather than
    # raise. A half-hour background crawl makes both non-optional.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn


# --- meta -------------------------------------------------------------------


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# --- tracks -----------------------------------------------------------------


def _artist_names(value: Any) -> str:
    """Flatten an artist credit to a display string.

    The store sits at the boundary with the raw API, which hands back artists
    as [{"name": ...}] in some responses and already-normalised ["name"] in
    others. Accept both rather than making every caller remember which.
    """
    if not value:
        return ""
    names = []
    for entry in value:
        if isinstance(entry, dict):
            name = entry.get("name")
        else:
            name = entry
        if name:
            names.append(str(name))
    return " & ".join(names)


def _album_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name")
    return value if isinstance(value, str) else None


def upsert_tracks(conn: sqlite3.Connection, tracks: Iterable[dict[str, Any]]) -> int:
    """Record basic track identity. Titles/artists are needed to label and to
    display results without a second API round-trip."""
    rows = [
        (t["videoId"], t.get("title"), _artist_names(t.get("artists")), _album_name(t.get("album")))
        for t in tracks
        if t.get("videoId")
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO track (video_id, title, artists, album) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(video_id) DO UPDATE SET "
        "  title   = COALESCE(excluded.title, track.title), "
        "  artists = COALESCE(NULLIF(excluded.artists, ''), track.artists), "
        "  album   = COALESCE(excluded.album, track.album)",
        rows,
    )
    conn.commit()
    return len(rows)


def get_track(conn: sqlite3.Connection, video_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM track WHERE video_id = ?", (video_id,)).fetchone()
    return dict(row) if row else None


# --- atlas ------------------------------------------------------------------


def record_playlist(
    conn: sqlite3.Connection,
    playlist_id: str,
    mood: str,
    title: str | None,
    tracks: list[dict[str, Any]],
    status: str = "ok",
) -> int:
    """Store one crawled mood playlist and check it off, in a single
    transaction -- a crash mid-playlist must not leave it marked done."""
    rows = [(t["videoId"], mood, playlist_id, title) for t in tracks if t.get("videoId")]
    with conn:
        if rows:
            conn.executemany(
                "INSERT INTO atlas_membership (video_id, mood, playlist_id, playlist_title) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(video_id, playlist_id, mood) DO NOTHING",
                rows,
            )
        conn.execute(
            "INSERT INTO atlas_crawl (playlist_id, mood, title, track_count, status, crawled_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(playlist_id, mood) DO UPDATE SET "
            "  title = excluded.title, track_count = excluded.track_count, "
            "  status = excluded.status, crawled_at = excluded.crawled_at",
            (playlist_id, mood, title, len(rows), status, time.time()),
        )
    upsert_tracks(conn, tracks)
    return len(rows)


def crawled_playlist_moods(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(playlist_id, mood) pairs already crawled successfully -- the resume point.

    Keyed on the pair, not the playlist: YouTube lists the same playlist under
    several moods, and each listing is a separate piece of evidence. Keying on
    playlist_id alone skipped 874 of 1,979 listings on a real crawl, throwing
    away every mood after the first one that happened to be crawled.

    Failures are excluded, so an interrupted or rate-limited listing is retried.
    """
    return {
        (r["playlist_id"], r["mood"])
        for r in conn.execute("SELECT playlist_id, mood FROM atlas_crawl WHERE status = 'ok'")
    }


def atlas_moods_for(conn: sqlite3.Connection, video_id: str) -> list[dict[str, Any]]:
    """Every mood playlist a song was found in."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT mood, playlist_id, playlist_title FROM atlas_membership WHERE video_id = ?",
            (video_id,),
        )
    ]


def atlas_mood_counts(conn: sqlite3.Connection, video_id: str) -> dict[str, int]:
    """mood -> how many distinct playlists of that mood contained the song.

    The count is the confidence signal: one Chill playlist is a rumour, six is
    a fact.
    """
    return {
        r["mood"]: r["n"]
        for r in conn.execute(
            "SELECT mood, COUNT(DISTINCT playlist_id) AS n FROM atlas_membership "
            "WHERE video_id = ? GROUP BY mood",
            (video_id,),
        )
    }


def atlas_video_ids(conn: sqlite3.Connection) -> set[str]:
    return {r["video_id"] for r in conn.execute("SELECT DISTINCT video_id FROM atlas_membership")}


def atlas_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    playlists = conn.execute(
        "SELECT COUNT(*) AS n, SUM(status = 'ok') AS ok FROM atlas_crawl"
    ).fetchone()
    return {
        "playlists_crawled": playlists["n"] or 0,
        "playlists_ok": playlists["ok"] or 0,
        "unique_tracks": conn.execute(
            "SELECT COUNT(DISTINCT video_id) AS n FROM atlas_membership"
        ).fetchone()["n"],
        "memberships": conn.execute("SELECT COUNT(*) AS n FROM atlas_membership").fetchone()["n"],
        "moods": {
            r["mood"]: r["n"]
            for r in conn.execute(
                "SELECT mood, COUNT(DISTINCT video_id) AS n FROM atlas_membership "
                "GROUP BY mood ORDER BY n DESC"
            )
        },
        "last_crawl_at": get_meta(conn, "atlas_last_crawl_at"),
    }


# --- mood labels ------------------------------------------------------------


def put_track_mood(
    conn: sqlite3.Connection,
    video_id: str,
    source: str,
    vector: dict[str, float],
    confidence: float,
) -> None:
    conn.execute(
        "INSERT INTO track_mood "
        "  (video_id, source, valence, energy, tension, depth, confidence, labeled_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(video_id, source) DO UPDATE SET "
        "  valence = excluded.valence, energy = excluded.energy, "
        "  tension = excluded.tension, depth = excluded.depth, "
        "  confidence = excluded.confidence, labeled_at = excluded.labeled_at",
        (
            video_id,
            source,
            vector["valence"],
            vector["energy"],
            vector["tension"],
            vector["depth"],
            confidence,
            time.time(),
        ),
    )
    conn.commit()


def put_track_moods(
    conn: sqlite3.Connection,
    source: str,
    entries: Iterable[tuple[str, dict[str, float], float]],
) -> int:
    """Bulk version of put_track_mood: one transaction for the whole batch.

    Materialising the atlas writes tens of thousands of rows; committing each
    one separately turns a two-second job into a several-minute one.
    """
    stamp = time.time()
    rows = [
        (vid, source, vec["valence"], vec["energy"], vec["tension"], vec["depth"], conf, stamp)
        for vid, vec, conf in entries
    ]
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO track_mood "
            "  (video_id, source, valence, energy, tension, depth, confidence, labeled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(video_id, source) DO UPDATE SET "
            "  valence = excluded.valence, energy = excluded.energy, "
            "  tension = excluded.tension, depth = excluded.depth, "
            "  confidence = excluded.confidence, labeled_at = excluded.labeled_at",
            rows,
        )
    return len(rows)


def get_track_moods(conn: sqlite3.Connection, video_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM track_mood WHERE video_id = ?", (video_id,))
    ]


def labeled_video_ids(conn: sqlite3.Connection, source: str | None = None) -> set[str]:
    if source is None:
        rows = conn.execute("SELECT DISTINCT video_id FROM track_mood")
    else:
        rows = conn.execute("SELECT video_id FROM track_mood WHERE source = ?", (source,))
    return {r["video_id"] for r in rows}


# --- lyrics -----------------------------------------------------------------


def put_lyrics(conn: sqlite3.Connection, video_id: str, text: str | None, source: str | None) -> None:
    conn.execute(
        "INSERT INTO lyrics (video_id, text, source, available, fetched_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(video_id) DO UPDATE SET text = excluded.text, source = excluded.source, "
        "  available = excluded.available, fetched_at = excluded.fetched_at",
        (video_id, text, source, 1 if text else 0, time.time()),
    )
    conn.commit()


def get_lyrics(conn: sqlite3.Connection, video_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM lyrics WHERE video_id = ?", (video_id,)).fetchone()
    return dict(row) if row else None


# --- library ----------------------------------------------------------------


def sync_library(conn: sqlite3.Connection, entries: Iterable[tuple[str, str, bool]]) -> int:
    """Replace the recorded library with (video_id, playlist_title, is_liked) rows."""
    rows = [(v, p, int(bool(liked)), time.time()) for v, p, liked in entries if v and p]
    with conn:
        conn.execute("DELETE FROM library_track")
        if rows:
            conn.executemany(
                "INSERT INTO library_track (video_id, playlist_title, is_liked, synced_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(video_id, playlist_title) DO NOTHING",
                rows,
            )
    return len(rows)


def library_video_ids(conn: sqlite3.Connection) -> set[str]:
    return {r["video_id"] for r in conn.execute("SELECT DISTINCT video_id FROM library_track")}


def library_playlists_for(conn: sqlite3.Connection, video_id: str) -> list[str]:
    return [
        r["playlist_title"]
        for r in conn.execute(
            "SELECT playlist_title FROM library_track WHERE video_id = ?", (video_id,)
        )
    ]


# --- history ----------------------------------------------------------------


def log_history(conn: sqlite3.Connection, items: Iterable[dict[str, Any]], observed_at: float | None = None) -> int:
    """Stamp a history snapshot with a real local time.

    The API only says "Today" or "Yesterday", so the timestamp we record here
    is the only clock the system will ever have.
    """
    stamp = observed_at if observed_at is not None else time.time()
    rows = [
        (item["videoId"], stamp, index, item.get("played"))
        for index, item in enumerate(items)
        if item.get("videoId")
    ]
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO history_log (video_id, observed_at, position, bucket) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(video_id, observed_at) DO NOTHING",
            rows,
        )
    return len(rows)


def recent_history(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Most recent distinct tracks, newest first, across all snapshots."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT video_id, MAX(observed_at) AS observed_at, MIN(position) AS position, "
            "       COUNT(*) AS snapshots "
            "FROM history_log GROUP BY video_id ORDER BY observed_at DESC, position ASC LIMIT ?",
            (limit,),
        )
    ]


# --- recommendations and feedback -------------------------------------------


def log_recommendations(
    conn: sqlite3.Connection,
    results: Iterable[dict[str, Any]],
    target: dict[str, float],
    feeling: str | None,
    arc: str | None,
) -> int:
    stamp = time.time()
    rows = [
        (
            r["videoId"], stamp, feeling, arc, r.get("slot"), r.get("score"),
            target["valence"], target["energy"], target["tension"], target["depth"],
        )
        for r in results
        if r.get("videoId")
    ]
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO recommendation "
            "  (video_id, served_at, feeling, arc, slot, score, valence, energy, tension, depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


# Reactions the listener states outright, and the ones only inferred from
# behaviour (see infer_implicit_feedback). Kept apart deliberately: only the
# stated ones are ever allowed to hard-exclude a song.
EXPLICIT_SOURCE = "explicit"
IMPLICIT_SOURCE = "implicit"

REACTION_PLAYED = "played"
REACTION_IGNORED = "ignored"

POSITIVE_REACTIONS = ("loved", "saved", REACTION_PLAYED)
NEGATIVE_REACTIONS = ("skipped", "wrong_mood", REACTION_IGNORED)

# How many history snapshots must have been taken *after* a song was
# recommended before its continued absence from them means anything. Below
# this, "not played" is far more likely to mean "the cron hasn't run yet" than
# "they didn't want it".
IGNORED_AFTER_SNAPSHOTS = 3


def put_feedback(conn: sqlite3.Connection, video_id: str, reaction: str, source: str = EXPLICIT_SOURCE) -> None:
    conn.execute(
        "INSERT INTO feedback (video_id, reaction, source, at) VALUES (?, ?, ?, ?)",
        (video_id, reaction, source, time.time()),
    )
    conn.commit()


def feedback_for(conn: sqlite3.Connection, video_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT reaction, source, at FROM feedback WHERE video_id = ? ORDER BY at DESC", (video_id,)
        )
    ]


def rejected_video_ids(conn: sqlite3.Connection) -> set[str]:
    """Songs the user has actively pushed away -- never serve these again.

    Deliberately explicit-only. A permanent hard exclusion is the strongest
    thing this system can do to a song, and inferred evidence has no business
    triggering it: "recommended, then never showed up in the history log" has
    plenty of innocent explanations (they never opened the playlist, the cron
    missed it, they listened on another device). Implicit feedback demotes in
    ranking instead -- see recommend.artist_affinity.
    """
    return {
        r["video_id"]
        for r in conn.execute(
            "SELECT DISTINCT video_id FROM feedback "
            "WHERE reaction IN ('skipped', 'wrong_mood') "
            "  AND (source IS NULL OR source != ?)",
            (IMPLICIT_SOURCE,),
        )
    }


def infer_implicit_feedback(
    conn: sqlite3.Connection, min_snapshots: int = IGNORED_AFTER_SNAPSHOTS
) -> dict[str, int]:
    """Learn from what was recommended by diffing it against what got played.

    `record_feedback` only ever fires when someone remembers to call it, which
    in practice is almost never -- so the explicit feedback table stays empty
    while two other tables quietly accumulate everything needed to infer the
    same thing: `recommendation` (what was served, and when) and `history_log`
    (what was actually listened to, with a real local timestamp).

    Joining them gives two signals for free:

    - **played**: the song turned up in the history log *after* it was
      recommended. Strong evidence the recommendation landed.
    - **ignored**: it didn't, and at least `min_snapshots` history snapshots
      have been taken since, so there was real listening to have shown up in.
      Weak evidence, and treated as such -- it demotes, never excludes.

    Idempotent: re-running records nothing new. Safe (and cheap -- pure local
    SQL, no network) to call on every recommendation.
    """
    rows = conn.execute(
        "WITH served AS ("
        "  SELECT video_id, MIN(served_at) AS first_served FROM recommendation"
        "   WHERE served_at IS NOT NULL GROUP BY video_id"
        ") "
        "SELECT s.video_id, "
        "  EXISTS (SELECT 1 FROM history_log h "
        "           WHERE h.video_id = s.video_id AND h.observed_at > s.first_served) AS played, "
        "  (SELECT COUNT(DISTINCT h2.observed_at) FROM history_log h2 "
        "    WHERE h2.observed_at > s.first_served) AS snapshots_since "
        "FROM served s"
    ).fetchall()

    already: dict[str, set[str]] = {}
    for r in conn.execute(
        "SELECT DISTINCT video_id, reaction FROM feedback WHERE source = ?", (IMPLICIT_SOURCE,)
    ):
        already.setdefault(r["video_id"], set()).add(r["reaction"])

    stamp = time.time()
    new_rows, retracted, pending = [], [], 0
    for row in rows:
        video_id, seen = row["video_id"], already.get(row["video_id"], set())
        if row["played"]:
            # A song inferred "ignored" that later got played was simply
            # inferred wrong -- drop the bad row rather than letting a stale
            # penalty sit alongside the evidence that contradicts it.
            if REACTION_IGNORED in seen:
                retracted.append(video_id)
            if REACTION_PLAYED not in seen:
                new_rows.append((video_id, REACTION_PLAYED, IMPLICIT_SOURCE, stamp))
        elif row["snapshots_since"] >= min_snapshots:
            if REACTION_IGNORED not in seen:
                new_rows.append((video_id, REACTION_IGNORED, IMPLICIT_SOURCE, stamp))
        else:
            pending += 1

    with conn:
        if retracted:
            conn.executemany(
                "DELETE FROM feedback WHERE video_id = ? AND reaction = ? AND source = ?",
                [(v, REACTION_IGNORED, IMPLICIT_SOURCE) for v in retracted],
            )
        if new_rows:
            conn.executemany(
                "INSERT INTO feedback (video_id, reaction, source, at) VALUES (?, ?, ?, ?)",
                new_rows,
            )

    return {
        "considered": len(rows),
        "played": sum(1 for r in new_rows if r[1] == REACTION_PLAYED),
        "ignored": sum(1 for r in new_rows if r[1] == REACTION_IGNORED),
        "retracted": len(retracted),
        "pending": pending,
    }


def feedback_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Per-song positive/negative reaction tallies, explicit and implicit.

    Both sources are pooled on purpose: a stated `loved` and an observed
    `played` are evidence of the same thing, differing in strength rather than
    in kind, and the caller (recommend.artist_affinity) bounds how far any
    amount of either can move a ranking.
    """
    counts: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT video_id, reaction FROM feedback"):
        tally = counts.setdefault(r["video_id"], {"positive": 0, "negative": 0})
        if r["reaction"] in POSITIVE_REACTIONS:
            tally["positive"] += 1
        elif r["reaction"] in NEGATIVE_REACTIONS:
            tally["negative"] += 1
    return counts


def feedback_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """How much has been learned so far, so an empty loop is visible."""
    by_source = {
        (r["source"] or EXPLICIT_SOURCE, r["reaction"]): r["n"]
        for r in conn.execute(
            "SELECT source, reaction, COUNT(*) AS n FROM feedback GROUP BY source, reaction"
        )
    }
    served = conn.execute(
        "SELECT COUNT(DISTINCT video_id) AS n FROM recommendation"
    ).fetchone()["n"]
    snapshots = conn.execute(
        "SELECT COUNT(DISTINCT observed_at) AS n FROM history_log"
    ).fetchone()["n"]
    return {
        "recommended_songs": served,
        "history_snapshots": snapshots,
        "explicit": {k[1]: v for k, v in by_source.items() if k[0] == EXPLICIT_SOURCE},
        "implicit": {k[1]: v for k, v in by_source.items() if k[0] == IMPLICIT_SOURCE},
    }


# --- tempo ------------------------------------------------------------------


def put_tempo(
    conn: sqlite3.Connection,
    video_id: str,
    bpm: float | None,
    status: str,
    deezer_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO track_tempo (video_id, bpm, status, deezer_id, resolved_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(video_id) DO UPDATE SET "
        "  bpm = excluded.bpm, status = excluded.status, "
        "  deezer_id = excluded.deezer_id, resolved_at = excluded.resolved_at",
        (video_id, bpm, status, deezer_id, time.time()),
    )
    conn.commit()


def get_tempo(conn: sqlite3.Connection, video_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM track_tempo WHERE video_id = ?", (video_id,)).fetchone()
    return dict(row) if row else None


def get_tempos(conn: sqlite3.Connection, video_ids: Iterable[str]) -> dict[str, float]:
    """Known tempos only -- ids absent from the result have no usable BPM."""
    ids = [v for v in dict.fromkeys(video_ids) if v]
    found: dict[str, float] = {}
    for start in range(0, len(ids), 900):
        window = ids[start : start + 900]
        placeholders = ",".join("?" * len(window))
        for row in conn.execute(
            f"SELECT video_id, bpm FROM track_tempo WHERE bpm IS NOT NULL AND video_id IN ({placeholders})",
            window,
        ):
            found[row["video_id"]] = row["bpm"]
    return found


def tempo_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM track_tempo GROUP BY status"
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    attempted = sum(by_status.values())
    return {
        "attempted": attempted,
        "with_bpm": by_status.get("ok", 0),
        "coverage": round(by_status.get("ok", 0) / attempted, 4) if attempted else 0.0,
        "by_status": by_status,
    }


# --- genre ------------------------------------------------------------------


def record_genre(conn: sqlite3.Connection, genre: str, video_ids: Iterable[str]) -> int:
    rows = [(v, genre) for v in video_ids if v]
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO genre_membership (video_id, genre) VALUES (?, ?) "
            "ON CONFLICT(video_id, genre) DO NOTHING",
            rows,
        )
    return len(rows)


def genres_for(conn: sqlite3.Connection, video_id: str) -> dict[str, int]:
    return {
        r["genre"]: 1
        for r in conn.execute("SELECT genre FROM genre_membership WHERE video_id = ?", (video_id,))
    }


def genre_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "tracks": conn.execute("SELECT COUNT(DISTINCT video_id) AS n FROM genre_membership").fetchone()["n"],
        "genres": {
            r["genre"]: r["n"]
            for r in conn.execute(
                "SELECT genre, COUNT(*) AS n FROM genre_membership GROUP BY genre ORDER BY n DESC"
            )
        },
    }
