"""BPM, via Deezer's public API.

YouTube Music exposes no tempo data whatsoever, so this comes from a second
source. Deezer's public API needs no key, no auth and no attribution, and
returns a `bpm` field on track detail.

Measured coverage on this library, and it matters: roughly 42% overall, but
6/6 on Pop and 1/6 on Punjabi and Bollywood. Verified that the misses are
genuinely `bpm: 0` in Deezer's data rather than failed matching -- every test
track resolved to the correct song, and scanning deeper into search results
finds nothing. So tempo is a real signal on part of the catalogue and simply
absent on the rest.

Two consequences shape the design:

  - Unknown tempo must never silently exclude a song. A tempo filter narrows
    the songs it can judge and leaves the rest ranked on everything else,
    saying so, because the alternative is a filter that quietly deletes an
    entire language from the results.
  - Tempo is NOT propagated by artist, unlike mood. An artist's songs share a
    sensibility but not a BPM; propagating it would invent data.
"""

import time
from typing import Any, Iterable

import graph
import match
import store

# The Deezer HTTP client, throttle and search now live in graph.py -- v6 made
# Deezer re-com's music graph rather than just a tempo source, and two modules
# holding two copies of the same client would have been the third copy of this
# plumbing. Re-exported here because callers and tests already reference them.
API = graph.API
THROTTLE = graph.THROTTLE
MAX_CANDIDATES = graph.MAX_CANDIDATES

STATUS_OK = "ok"
STATUS_NO_BPM = "no_bpm"      # matched the song; Deezer has no tempo for it
STATUS_NO_MATCH = "no_match"  # nothing on Deezer resembling this song

# Credit/title matching lives in match.py -- shared with signals.py and, from
# v6, with the music graph, which compares Deezer credits against provider
# credits in exactly the same way. `_same_title` used to `import signals`
# inside the function body to dodge a circular import -- a low-level module
# reaching up into a higher-level one. match.py depends on nothing.
_artist_matches = match.artist_matches
_same_title = match.same_title


def lookup(title: str, artist: str | None = None, sleep=time.sleep) -> tuple[float | None, str, int | None]:
    """Find a tempo for one song. Returns (bpm, status, deezer_id).

    Searching and credit-matching are `graph.search_tracks`; this adds the
    part that is specific to tempo -- scanning the matched hits for a track
    that actually carries a BPM, because Deezer often has the right song and
    no tempo for it (`bpm: 0`), which is a different outcome from not having
    the song at all.
    """
    if not title:
        return None, STATUS_NO_MATCH, None

    rows = graph.search_tracks(title, artist, sleep=sleep)
    if not rows:
        return None, STATUS_NO_MATCH, None

    matching = [r for r in rows if r.get("matched")]

    # An unmatched hit is still the best guess at which Deezer record this is,
    # and is recorded alongside the no-match status rather than discarded.
    first_id = (matching[0] if matching else rows[0]).get("id")

    for hit in matching:
        detail = graph.track_detail(hit["id"], sleep=sleep)
        if detail is None:
            continue
        bpm = detail.get("bpm")
        if bpm:
            return float(bpm), STATUS_OK, hit["id"]

    return None, (STATUS_NO_BPM if matching else STATUS_NO_MATCH), first_id


def get_or_fetch(conn: Any, video_id: str, title: str, artist: str | None, sleep=time.sleep) -> float | None:
    """Cached tempo, hitting Deezer only on a genuine first look.

    Negative results are cached too -- rediscovering that Deezer has no tempo
    for a song costs two requests every time otherwise.
    """
    cached = store.get_tempo(conn, video_id)
    if cached is not None:
        return cached["bpm"]

    bpm, status, deezer_id = lookup(title, artist, sleep=sleep)
    store.put_tempo(conn, video_id, bpm, status, deezer_id)
    return bpm


def relative_distance(a: float, b: float) -> float:
    """Tempo distance, tolerant of half- and double-time.

    A 170bpm drum-and-bass track and an 85bpm hip-hop track sit on the same
    pulse; treating them as maximally far apart is musically wrong. Compare
    against b, b/2 and b*2 and keep the closest reading.
    """
    if not a or not b:
        return 1.0
    best = min(abs(a - candidate) / max(a, candidate) for candidate in (b, b / 2, b * 2))
    return min(1.0, best)


def similarity(a: float | None, b: float | None) -> float | None:
    """0..1 tempo agreement, or None when either song's tempo is unknown.

    None is deliberately distinct from 0.0: "we don't know" must not be
    scored as "definitely wrong".
    """
    if not a or not b:
        return None
    return 1.0 - relative_distance(a, b)


def in_range(bpm: float | None, low: float | None, high: float | None) -> bool | None:
    """Whether a tempo falls in a range. None when unknown."""
    if not bpm:
        return None
    if low is not None and bpm < low:
        return False
    if high is not None and bpm > high:
        return False
    return True


def backfill(conn: Any, rows: Iterable[dict[str, Any]], sleep=time.sleep, on_progress=None) -> dict[str, int]:
    """Resolve tempo for many songs, skipping anything already attempted."""
    stats = {"resolved": 0, "no_bpm": 0, "no_match": 0, "cached": 0}
    for index, row in enumerate(rows, start=1):
        video_id = row["video_id"]
        if store.get_tempo(conn, video_id) is not None:
            stats["cached"] += 1
            continue
        bpm, status, deezer_id = lookup(row.get("title"), row.get("artists"), sleep=sleep)
        store.put_tempo(conn, video_id, bpm, status, deezer_id)
        stats["resolved" if status == STATUS_OK else status] += 1
        if on_progress:
            on_progress({**stats, "index": index, "title": row.get("title"), "bpm": bpm})
        sleep(THROTTLE)
    return stats
