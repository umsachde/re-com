"""The second graph source: MusicBrainz identity, ListenBrainz adjacency.

**Why a second source at all.** §7.2's quality baseline produced two numbers
that no amount of Deezer coverage can move. 87% of Spotify's recommendations
rested on a *single* signal, because with Spotify's own discovery endpoints
revoked (`graph.py`'s header has the measurements) the graph is the only voice
in the room -- and one source cannot corroborate itself, so agreement-based
ranking was barely ranking there. And same-artist seeds returned 90% the same
songs, because one artist-centric source was the only thing shaping the result.
Both are single-source problems. Both need a second, independent source.

**Why ListenBrainz, having already rejected it.** `graph.py`'s header records
ListenBrainz as probed and rejected in 2026-08: `similar-recordings` empty for
all six test tracks including *Blinding Lights*, at 12-19s per call. That
rejection was real but it tested the wrong thing. The v1 `similar-recordings`
path now 404s outright, and re-com's graph is **artist-centric** anyway -- the
whole point of `graph.py`'s "there is no track-level radio" note. Re-probed on
2026-09-11 against the *labs* API's `similar-artists` instead:

    AP Dhillon      0.5s   33 neighbours   Gurinder Gill, Shubh, Diljit Dosanjh
    Diljit Dosanjh  0.6s  100 neighbours   Sidhu Moose Wala, Harrdy Sandhu
    Arijit Singh    0.7s  100 neighbours   Shreya Ghoshal, Atif Aslam, KK
    The Weeknd      0.8s  100 neighbours   Daft Punk, Kendrick Lamar

Sub-second, populated, and culturally correct on the Punjabi/Bollywood
catalogue this library is mostly made of -- the same bar `graph.py` set for
Deezer and the same bar this source is held to.

**Independence, measured, because that is the only thing that justifies it.**
Against Deezer's related-artists over eight seeds: overall Jaccard **0.137**.
On the Punjabi core, 12-15 of Deezer's 20 are corroborated by ListenBrainz
(~60-65%) while 393 new artists appear across the eight -- Manni Sandhu, Prem
Dhillon, Sunny Malton, Harnoor, KK, Sunidhi Chauhan, A. R. Rahman. High enough
agreement to be evidence, low enough to be new information. Two honest
caveats: that 0.137 is flattered by size asymmetry (ListenBrainz returns up to
100, Deezer a fixed 20), and coverage is **not** universal -- Dua Lipa returned
zero neighbours. This source supplements Deezer; it does not replace it, and a
seed with no ListenBrainz answer must degrade to Deezer-only silently.

**What this does NOT fix.** ListenBrainz returns artists, never tracks. Turning
a neighbour into candidates still crosses back into Deezer's catalogue via
`graph.artist_tracks`. So this makes *adjacency* plural, not the graph: Deezer
remains a single point of failure for the catalogue itself. §7.3's "Deezer is
a single point of failure" is narrowed by this work, not closed by it.

**MusicBrainz is the bottleneck and is treated as one.** It enforces 1 req/sec
and 503s immediately past it (re-confirmed 2026-09-11 -- a 1.1s spacing still
drew 503s, 1.2s with backoff did not). Identity lookups are therefore bought at
a full second each, done per *artist* rather than per track, and cached
permanently: MBIDs are stable by design.
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

MB_API = "https://musicbrainz.org/ws/2"
LB_API = "https://api.listenbrainz.org/1"
LB_LABS = "https://labs.api.listenbrainz.org"

# MusicBrainz requires a contactable User-Agent and will block generic ones.
USER_AGENT = "re-com/0.3 (+https://github.com/umsachde/re-com)"

# MusicBrainz's published limit is 1 req/sec. 1.1s still drew 503s on
# measurement; 1.2s did not. The margin is deliberate -- a 503 here costs a
# retry plus a backoff, which is far more expensive than the extra 200ms.
MB_THROTTLE = 1.2
LB_THROTTLE = 0.15
TIMEOUT = 20

# ListenBrainz exposes several precomputed datasets under opaque names. This is
# the widest session-based one; `filter_True` drops artists the reference
# already appears under, `skip_30` ignores listens under 30s.
LB_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5"
    "_threshold_10_limit_100_filter_True_skip_30"
)

# A *different* enumeration from LB_ALGORITHM: `similar-recordings` rejects the
# artist names with a 400 listing its seven permitted values, which is how the
# 2026-08 probe and §7.3's re-probe both mistook a bad request for no coverage.
LB_RECORDING_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5_threshold_15_limit_50_skip_30"
)

_EP_LB_RELATED = "lb_artist_related"
_EP_LB_SIMILAR_RECORDINGS = "lb_similar_recordings"


def token() -> str | None:
    """The ListenBrainz user token, read per call so tests and reconfigs see changes."""
    value = os.environ.get("LISTENBRAINZ_TOKEN", "").strip()
    return value or None

_NET_ERRORS = (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError)


class _Unavailable:
    """"We could not ask", as distinct from "we asked and the answer was no".

    This distinction is load-bearing rather than pedantic. Both caches here
    store negative results deliberately -- an artist MusicBrainz does not carry
    must not cost a throttled second on every pass forever. But MusicBrainz
    503s the instant you exceed 1 req/sec, and it 503s often. Folding that into
    the same `None` as a real miss means one transient rate-limit permanently
    poisons the cache for that artist, and the poisoned row is indistinguishable
    from a genuine one afterwards -- exactly the failure `tests/conftest.py`'s
    header warns about. Caught on the first live run, where a 503 had silently
    turned Arijit Singh into an artist with no neighbours.
    """

    def __bool__(self) -> bool:
        return False


UNAVAILABLE = _Unavailable()


def _get_safe(
    url: str,
    tries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    auth: str | None = None,
) -> Any:
    """A GET that reports *why* it has no answer.

    Returns the payload, or `UNAVAILABLE` when the service could not be reached
    at all. Retries only on 503, which for MusicBrainz means "you went too
    fast" rather than "this does not exist" -- the one failure worth a retry.
    A 401 from a bad token is UNAVAILABLE too, so it never caches as a miss.
    """
    headers = {"User-Agent": USER_AGENT}
    if auth:
        headers["Authorization"] = f"Token {auth}"
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code != 503 or attempt == tries - 1:
                return UNAVAILABLE
            sleep(MB_THROTTLE * (attempt + 2))
        except _NET_ERRORS:
            return UNAVAILABLE
    return UNAVAILABLE


# --- artist identity (MusicBrainz) ------------------------------------------


def resolve_artist(
    conn: Any, artist: str, sleep: Callable[[float], None] = time.sleep
) -> dict[str, Any] | None:
    """Cached artist name -> MusicBrainz identity. None when nothing matches.

    Negative results are cached for the same reason `graph.resolve` caches
    them: an artist MusicBrainz genuinely does not carry would otherwise cost a
    full throttled second on every pass forever.
    """
    if not artist or not artist.strip():
        return None
    artist_key = artist.strip().lower()

    cached = graph_store.get_brainz_artist(conn, artist_key)
    if cached is not None:
        if cached["status"] != graph_store.STATUS_OK:
            return None
        return {"mbid": cached["mbid"], "name": cached["name"]}

    sleep(MB_THROTTLE)
    query = urllib.parse.quote(f'artist:"{artist.strip()[:180]}"')
    payload = _get_safe(f"{MB_API}/artist?query={query}&fmt=json&limit=5", sleep=sleep)
    if payload is UNAVAILABLE:
        return None  # Could not ask. Leave the cache empty so the next pass retries.
    hits = (payload or {}).get("artists") or []

    # The credit gate is match.py's, the same one Deezer resolution uses, so
    # the two sources cannot disagree about what counts as the right artist.
    best = next((h for h in hits if match.artist_matches(h.get("name"), artist)), None)
    graph_store.put_brainz_artist(
        conn,
        artist_key,
        mbid=best.get("id") if best else None,
        name=best.get("name") if best else None,
        status=graph_store.STATUS_OK if best else graph_store.STATUS_NO_MATCH,
    )
    return {"mbid": best["id"], "name": best.get("name")} if best else None


# --- adjacency (ListenBrainz) -----------------------------------------------


def related_artists(
    conn: Any, mbid: str, sleep: Callable[[float], None] = time.sleep
) -> list[dict[str, Any]]:
    """Artists adjacent to this MBID, best first. Cached, including empty.

    Caching the empty answer matters more here than for Deezer: ListenBrainz
    coverage is genuinely partial (Dua Lipa returns nothing), so "no neighbours"
    is a real, permanent answer for some artists and must not be re-fetched
    forever.
    """
    if not mbid:
        return []
    if graph_store.was_fetched(conn, _EP_LB_RELATED, mbid):
        return graph_store.get_brainz_related(conn, mbid)

    sleep(LB_THROTTLE)
    payload = _get_safe(
        f"{LB_LABS}/similar-artists/json?artist_mbids={urllib.parse.quote(mbid)}"
        f"&algorithm={LB_ALGORITHM}",
        sleep=sleep,
    )
    if payload is UNAVAILABLE:
        return []  # Unreachable, not childless. No fetch is recorded.
    rows = [
        {"mbid": row.get("artist_mbid"), "name": row.get("name"), "score": row.get("score")}
        for row in (payload if isinstance(payload, list) else [])
        if isinstance(row, dict) and row.get("artist_mbid") and row.get("name")
    ]
    graph_store.put_brainz_related(conn, mbid, rows)
    graph_store.record_fetch(conn, _EP_LB_RELATED, mbid, graph_store.STATUS_OK, len(rows))
    return rows


def related_artist_names(
    conn: Any, artist: str, limit: int = 10, sleep: Callable[[float], None] = time.sleep
) -> list[str]:
    """Artist name -> adjacent artist names. The whole source in one call.

    Names rather than MBIDs because the consumer is `graph.neighbours`, which
    needs Deezer artist ids and gets them by name. Returning MBIDs would imply
    an identity bridge between MusicBrainz and Deezer that does not exist.
    """
    identity = resolve_artist(conn, artist, sleep=sleep)
    if not identity or not identity.get("mbid"):
        return []
    return [r["name"] for r in related_artists(conn, identity["mbid"], sleep=sleep)[:limit] if r.get("name")]


# --- track-level similarity (PLAN.md 7.12) ----------------------------------


def resolve_recording(
    conn: Any, title: str, artist: str | None, sleep: Callable[[float], None] = time.sleep
) -> dict[str, Any] | None:
    """Cached title+artist -> *canonical* recording MBID. None without a token.

    Canonical is the whole point. Similarity is keyed on one recording per
    song, and MusicBrainz search returns whichever of its duplicates scores
    highest: 11 of Blinding Lights' 12 recording MBIDs have zero neighbours.
    ListenBrainz's own lookup maps to the one that has them, but needs a user
    token -- so no token means this source is off, not that nothing matched,
    and nothing is cached.
    """
    auth = token()
    song_key = match.song_key(title or "")
    if not auth or not song_key or not artist or not artist.strip():
        return None
    artist_key = artist.strip().lower()

    cached = graph_store.get_brainz_recording(conn, song_key, artist_key)
    if cached is not None:
        if cached["status"] != graph_store.STATUS_OK:
            return None
        return {"mbid": cached["mbid"], "title": cached["title"], "artist_name": cached["artist_name"]}

    sleep(LB_THROTTLE)
    query = urllib.parse.urlencode({"artist_name": artist.strip(), "recording_name": title.strip()})
    payload = _get_safe(f"{LB_API}/metadata/lookup/?{query}", sleep=sleep, auth=auth)
    if payload is UNAVAILABLE:
        return None
    # A miss is `{}` with a 200, not a 404.
    hit = payload if isinstance(payload, dict) and payload.get("recording_mbid") else None
    graph_store.put_brainz_recording(
        conn,
        song_key,
        artist_key,
        mbid=hit["recording_mbid"] if hit else None,
        title=hit.get("recording_name") if hit else None,
        artist_name=hit.get("artist_credit_name") if hit else None,
        status=graph_store.STATUS_OK if hit else graph_store.STATUS_NO_MATCH,
    )
    if not hit:
        return None
    return {"mbid": hit["recording_mbid"], "title": hit.get("recording_name"), "artist_name": hit.get("artist_credit_name")}


def similar_recordings(
    conn: Any, mbid: str, sleep: Callable[[float], None] = time.sleep
) -> list[dict[str, Any]]:
    """Recordings similar to this canonical MBID, best first. Cached, including empty.

    Unlike every other graph signal this one distinguishes two songs by the
    same artist: Channa Mereya and Kesariya overlap 0.00 here against 0.90
    artist-centrically. Coverage is thin on this library's Bollywood catalogue
    (Kesariya: one neighbour) and dense on Western pop (Blinding Lights: 100).
    """
    if not mbid:
        return []
    if graph_store.was_fetched(conn, _EP_LB_SIMILAR_RECORDINGS, mbid):
        return graph_store.get_brainz_similar_recordings(conn, mbid)

    sleep(LB_THROTTLE)
    payload = _get_safe(
        f"{LB_LABS}/similar-recordings/json?recording_mbids={urllib.parse.quote(mbid)}"
        f"&algorithm={LB_RECORDING_ALGORITHM}",
        sleep=sleep,
    )
    if payload is UNAVAILABLE:
        return []
    rows = [
        {
            "mbid": row.get("recording_mbid"),
            "title": row.get("recording_name"),
            "artist_name": row.get("artist_credit_name"),
            "score": row.get("score"),
        }
        for row in (payload if isinstance(payload, list) else [])
        if isinstance(row, dict) and row.get("recording_mbid") and row.get("recording_name")
    ]
    graph_store.put_brainz_similar_recordings(conn, mbid, rows)
    graph_store.record_fetch(conn, _EP_LB_SIMILAR_RECORDINGS, mbid, graph_store.STATUS_OK, len(rows))
    return rows


def similar_tracks(
    conn: Any, title: str, artist: str | None, limit: int = 10, sleep: Callable[[float], None] = time.sleep
) -> list[dict[str, Any]]:
    """Seed song -> similar songs as title/artist text. The track source in one call."""
    identity = resolve_recording(conn, title, artist, sleep=sleep)
    if not identity:
        return []
    return similar_recordings(conn, identity["mbid"], sleep=sleep)[:limit]
