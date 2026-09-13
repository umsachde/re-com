"""Track-level similarity from last.fm -- the South Asian catalogue ListenBrainz cannot reach.

**Why a second track-level source.** PLAN.md 7.12 wired in ListenBrainz's
`similar-recordings`, the first signal that tells two songs by one artist
apart. It works, and it is nearly empty on the half of this library that
matters most. Probed 2026-09-12 across all seven of its algorithms and every
MusicBrainz recording of each song: Brown Munde 0, 295 0, Kesariya 1. That is
ListenBrainz's listener base, not re-com's lookup, and no configuration moves it.

last.fm's `track.getSimilar`, probed the same day on the same seeds:

    Brown Munde    50   (LB 0)     Shubh, Karan Aujla, Diljit Dosanjh
    295            50   (LB 0)     Shubh, Diljit Dosanjh
    Tum Hi Ho      50   (LB 8)     Mohit Chauhan, Roop Kumar Rathod
    Excuses vs Brown Munde overlap 0.27, against 0.97 artist-centrically

~0.5s per call, at most 2 of 50 neighbours by the seed's own artist.

**What it does not cover.** Recent Bollywood film songs -- Channa Mereya,
Kesariya -- return zero under *every* last.fm spelling of them. Their listeners
are split across lo-fi flips, "(From <film>)" titles and MP3-site rips, so no
single entry clears the similarity threshold. Neither source covers them.

**Identity is last.fm's own.** It matches on artist + title text with
`autocorrect=1`, so there is no MBID step and no canonical-recording problem.

**Error contract, measured.** error 6 ("Track not found", a 200) and an empty
`track` list are real answers and are cached. Anything else -- a 403 with
error 10 for a bad key, rate limiting, a network failure -- means the service
could not be asked and caches nothing; PRs #12 and #13 fixed exactly that
mistake in the Deezer caches.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import graph_store
import match

API = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "re-com/0.3 (+https://github.com/umsachde/re-com)"
THROTTLE = 0.25
TIMEOUT = 20

_ERR_TRACK_NOT_FOUND = 6
_EP_SIMILAR = "lastfm_similar"

_NET_ERRORS = (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError)


def api_key() -> str | None:
    """The last.fm API key, read per call so tests and reconfigs see changes."""
    value = os.environ.get("LASTFM_API_KEY", "").strip()
    return value or None


def _get(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def _similar_rows(title: str, artist: str, key: str, limit: int) -> list[dict[str, Any]] | None:
    """Similar-track rows, or None when last.fm could not be asked."""
    query = urllib.parse.urlencode({
        "method": "track.getSimilar", "api_key": key, "format": "json",
        "autocorrect": 1, "track": title, "artist": artist, "limit": limit,
    })
    try:
        payload = _get(f"{API}?{query}")
    except _NET_ERRORS:
        return None
    if not isinstance(payload, dict):
        return None
    if "error" in payload:
        return [] if payload.get("error") == _ERR_TRACK_NOT_FOUND else None
    tracks = (payload.get("similartracks") or {}).get("track")
    if not isinstance(tracks, list):
        return None
    return [
        {"title": t.get("name"), "artist_name": (t.get("artist") or {}).get("name"), "match": t.get("match")}
        for t in tracks
        if isinstance(t, dict) and t.get("name") and (t.get("artist") or {}).get("name")
    ]


def similar_tracks(
    conn: Any,
    title: str,
    artist: str | None,
    limit: int = 10,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Seed song -> similar songs as title/artist text, best first. Cached, including empty.

    Fetches a fixed 50 regardless of `limit` so the cache answers any later
    caller that wants more, instead of being keyed on how many were asked for.
    """
    key = api_key()
    song_key = match.song_key(title or "")
    if not key or not song_key or not artist or not artist.strip():
        return []
    cache_key = f"{song_key}|{artist.strip().lower()}"

    if graph_store.was_fetched(conn, _EP_SIMILAR, cache_key):
        return graph_store.get_lastfm_similar(conn, cache_key)[:limit]

    sleep(THROTTLE)
    rows = _similar_rows(title.strip(), artist.strip(), key, limit=50)
    if rows is None:
        return []
    graph_store.put_lastfm_similar(conn, cache_key, rows)
    graph_store.record_fetch(conn, _EP_SIMILAR, cache_key, graph_store.STATUS_OK, len(rows))
    return rows[:limit]
