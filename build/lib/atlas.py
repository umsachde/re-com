"""Crawling YouTube Music's own mood playlists into a local mood corpus.

YouTube Music exposes no audio features at all -- no tempo, key, valence or
energy (verified against the live API). What it does expose is a mood taxonomy:
`get_mood_categories()` returns 13 "Moods & moments", and each one lists
hundreds of editorial playlists. Membership in those playlists is the cheapest
mood evidence available anywhere, so we crawl it once and keep it.

Scale, measured: 2,223 playlists across the 13 moods, of which ~1,979 fall
under the 11 moods that have an honest position in the vector space. At roughly
a second each that's a ~35 minute crawl, which is why this is a resumable
background job and never something a tool call does inline.

It is NOT sufficient on its own. A 60-playlist sample covered only 4.1% of this
user's liked library, and the misses concentrate on non-English catalogue that
YouTube's mood playlists barely touch. The atlas is the cheap broad prior; the
lyrics and language layers are what make it usable.
"""

import random
import time
from typing import Any, Callable, Iterable

import requests
from ytmusicapi.exceptions import YTMusicError, YTMusicServerError

import moodspace
import store

MOOD_SECTION = "Moods & moments"

# Deliberately gentle. 2,000+ sequential requests is exactly the shape of
# traffic that earns a rate limit, and being throttled halfway costs far more
# than the minutes saved by hurrying.
THROTTLE_SECONDS = 0.25
THROTTLE_JITTER = 0.15
RATE_LIMIT_BACKOFF = 30.0
MAX_RATE_LIMIT_RETRIES = 5

# One page per playlist. Mood playlists run ~80 tracks; paginating deeper
# multiplies the crawl's cost for rapidly diminishing evidence.
TRACKS_PER_PLAYLIST = 100

_TRANSIENT = (YTMusicError, requests.exceptions.RequestException)


def _is_rate_limited(err: Exception) -> bool:
    return isinstance(err, YTMusicServerError) and "HTTP 429" in str(err)


def _pause(seconds: float, sleep: Callable[[float], None]) -> None:
    if seconds > 0:
        sleep(seconds)


def mood_params(yt: Any) -> dict[str, str]:
    """Map mood name -> the opaque params token needed to list its playlists."""
    categories = yt.get_mood_categories()
    return {c["title"]: c["params"] for c in categories.get(MOOD_SECTION, []) if c.get("params")}


def enumerate_playlists(
    yt: Any,
    moods: Iterable[str] = moodspace.CRAWLABLE_MOODS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[tuple[str, str, str | None]]:
    """List every (mood, playlist_id, title) worth crawling.

    Only moods with an anchor in the vector space are included -- crawling
    Christmas would cost four minutes to collect songs we could never place.
    """
    params = mood_params(yt)
    found: list[tuple[str, str, str | None]] = []
    for mood in moods:
        token = params.get(mood)
        if not token:
            continue
        try:
            playlists = yt.get_mood_playlists(token)
        except _TRANSIENT:
            continue
        for p in playlists:
            if p.get("playlistId"):
                found.append((mood, p["playlistId"], p.get("title")))
        _pause(THROTTLE_SECONDS, sleep)
    return found


def crawl(
    yt: Any,
    conn: Any,
    moods: Iterable[str] = moodspace.CRAWLABLE_MOODS,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    limit: int | None = None,
) -> dict[str, Any]:
    """Crawl mood playlists into the store, resuming where a previous run left off.

    Every playlist is committed as it lands, so an interrupted crawl loses at
    most one playlist. Playlists that fail are recorded as failures rather than
    successes, which means the next run retries exactly those.
    """
    todo = enumerate_playlists(yt, moods, sleep=sleep)
    already = store.crawled_playlist_moods(conn)
    remaining = [entry for entry in todo if (entry[1], entry[0]) not in already]
    pending = remaining[:limit] if limit is not None else remaining

    stats = {
        "total": len(todo),
        "already_crawled": len(todo) - len(remaining),
        "deferred": len(remaining) - len(pending),  # cut off by --limit, not done
        "ok": 0,
        "failed": 0,
        "tracks": 0,
    }

    for index, (mood, playlist_id, title) in enumerate(pending, start=1):
        tracks, status = _fetch_playlist(yt, playlist_id, sleep)
        if status == "ok":
            stats["tracks"] += store.record_playlist(conn, playlist_id, mood, title, tracks)
            stats["ok"] += 1
        else:
            store.record_playlist(conn, playlist_id, mood, title, [], status=status)
            stats["failed"] += 1

        if on_progress:
            on_progress({**stats, "index": index, "pending": len(pending), "mood": mood, "title": title, "status": status})
        _pause(THROTTLE_SECONDS + random.random() * THROTTLE_JITTER, sleep)

    store.set_meta(conn, "atlas_last_crawl_at", str(time.time()))
    return stats


def _fetch_playlist(yt: Any, playlist_id: str, sleep: Callable[[float], None]) -> tuple[list[dict], str]:
    """Fetch one playlist, backing off and retrying only on rate limits.

    Rate limiting is temporary and worth waiting out; a deleted or private
    playlist is permanent and retrying it just burns the budget.
    """
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        try:
            result = yt.get_playlist(playlist_id, limit=TRACKS_PER_PLAYLIST)
            return [t for t in result.get("tracks", []) if t.get("videoId")], "ok"
        except Exception as err:  # noqa: BLE001 - classified immediately below
            if _is_rate_limited(err):
                _pause(RATE_LIMIT_BACKOFF * (2**attempt), sleep)
                continue
            if isinstance(err, _TRANSIENT):
                return [], "failed"
            raise
    return [], "rate_limited"


def materialize_moods(conn: Any) -> int:
    """Collapse raw playlist membership into one atlas mood vector per song.

    Kept as a separate pass rather than done during the crawl so the anchor
    table can be retuned and re-applied in seconds without re-crawling.
    """
    rows = conn.execute(
        "SELECT video_id, mood, COUNT(DISTINCT playlist_id) AS n "
        "FROM atlas_membership GROUP BY video_id, mood"
    ).fetchall()

    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        counts.setdefault(row["video_id"], {})[row["mood"]] = row["n"]

    entries = []
    for video_id, mood_counts in counts.items():
        result = moodspace.from_atlas_counts(mood_counts)
        if result is not None:
            entries.append((video_id, result[0], result[1]))
    return store.put_track_moods(conn, "atlas", entries)
