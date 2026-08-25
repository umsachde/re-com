"""The neutral music graph: similarity and adjacency that belong to no provider.

**Why this exists.** re-com's v1 engine drew candidates from the provider's own
discovery endpoints, and v3 proved that cannot carry a provider-agnostic app.
Measured against the real Spotify app registration (2026-08-23):
`/recommendations` 404s, `artist_related_artists` 403s, `artist_top_tracks`
403s, `audio_features` 403s, and other users' playlists cannot be read at all.
Two of three signals are unbuildable, so `recommend_from_song` on Spotify
returned **zero songs**. YouTube Music, meanwhile, has no official API at all.

Provider discovery endpoints differ wildly, get revoked unilaterally, and are
nobody's contract. So the split v6 makes is:

    provider  ->  whose taste this is   (library, history, playlist writes)
    graph     ->  what sounds like what (similarity, adjacency, mood corpus)

**Deezer is the graph, chosen on measurement rather than preference.**
ListenBrainz/MusicBrainz was probed first and rejected on evidence: MusicBrainz
resolves identity excellently (7/8 test tracks, every Punjabi/Bollywood one at
score 100 -- worth remembering if a second graph is ever needed), but
ListenBrainz's `similar-recordings` returned empty for all six resolved tracks
including *Blinding Lights*, at 12-19s per call, and MusicBrainz 503s under
1 req/sec. Unusable on a live path.

Deezer needs no key, no auth and no attribution -- the same reasons `tempo.py`
already chose it -- and its related-artists are culturally correct on the part
of this library that matters most: AP Dhillon -> Diljit Dosanjh, Shubh, Garry
Sandhu, Karan Aujla, Amrinder Gill.

**The cost, stated plainly: there is no track-level radio.** `/track/{id}/radio`
and `/track/{id}/related` do not exist (both probed, InvalidQueryException).
YouTube's per-track radio is re-com's single strongest signal and has no Deezer
equivalent, so graph similarity is **artist-centric**. That is a genuine
quality regression on YouTube and is why native signals stay in the mix rather
than being replaced -- see `signals.py`'s capability gating.

**Do not confuse graph coverage with BPM coverage.** `tempo.py` reaches only
6-16% of the Punjabi/Bollywood catalogue, but that is Deezer missing *tempo
data*, not missing *songs*: those tracks resolve to the correct Deezer record
and simply carry `bpm: 0`. Resolution and adjacency are far better covered.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

import graph_store
import match

API = "https://api.deezer.com"
USER_AGENT = "re-com/0.3 (+https://github.com/umsachde/re-com)"

# Deezer permits roughly 50 requests per 5 seconds. Stay well under it.
THROTTLE = 0.12
TIMEOUT = 15

# How many search hits to inspect when matching a song. Kept at tempo.py's
# original value -- this is the same search, now shared.
MAX_CANDIDATES = 4

# Endpoint names for graph_fetch bookkeeping.
_EP_RELATED = "artist_related"
_EP_TOP = "artist_top"
_EP_RADIO = "artist_radio"

KIND_TOP = "top"
KIND_RADIO = "radio"

# Network failures that mean "this signal is unavailable right now", never
# "abort the request". Same partial-results philosophy as signals.py.
_NET_ERRORS = (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError)


def _get(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def _get_safe(url: str) -> Any | None:
    """A GET whose failure is a missing signal rather than an error."""
    try:
        return _get(url)
    except _NET_ERRORS:
        return None


def _data(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    return rows if isinstance(rows, list) else []


# --- track identity ---------------------------------------------------------


def _track_row(hit: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Deezer track object into the shape the store and signals use."""
    artist = hit.get("artist") or {}
    return {
        "id": hit.get("id"),
        "title": hit.get("title"),
        "artist_name": artist.get("name"),
        "artist_id": artist.get("id"),
    }


def search_tracks(title: str, artist: str | None = None, sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """Deezer hits for a song, best match first. Each row carries `matched`.

    The single Deezer track-matching entry point: `tempo.py` scans the result
    for a usable BPM, the graph takes the best row as the song's identity.
    One search, one set of matching rules (`match.py`), one HTTP client.

    The title-only fallback is why `match.same_title` guards it. The artist
    gate alone is too strict for real YouTube credits -- compilation uploads
    ("Billboard Top 100 Hits") and odd separators ("Shankar Mahadevan | Alyssa
    Men") match nothing on Deezer, which reported 318 library songs unmatched
    when most were findable by title. Reclassifying them yielded only +24
    actual BPMs, so the fallback fixed the accounting more than the coverage --
    but it is still the correct behaviour, and identity matters to the graph
    even where tempo does not exist.

    `matched=False` rows are hits the credit gate rejected. They are returned
    rather than dropped because a rejected hit is still the best *guess* at
    which Deezer record this is, which `tempo.py` records alongside a
    no-match status. Callers that need real identity -- `resolve`, and every
    graph signal -- must use only `matched=True` rows.
    """
    if not title:
        return []

    hits = _search(f"{title} {artist}".strip(), sleep) if artist else []
    matching = [h for h in hits if match.artist_matches((h.get("artist") or {}).get("name", ""), artist)]

    if not matching:
        for hit in _search(title, sleep):
            if match.same_title(hit.get("title"), title):
                matching.append(hit)

    rows = [{**_track_row(h), "matched": True} for h in matching]
    if not matching and hits:
        rows.append({**_track_row(hits[0]), "matched": False})
    return rows


def _search(query: str, sleep: Callable[[float], None]) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    encoded = urllib.parse.quote(query.strip()[:180])
    return _data(_get_safe(f"{API}/search?q={encoded}&limit={MAX_CANDIDATES}"))


def track_detail(track_id: int, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any] | None:
    """Full track record, including the `bpm` field `tempo.py` wants."""
    sleep(THROTTLE)
    detail = _get_safe(f"{API}/track/{track_id}")
    return detail if isinstance(detail, dict) else None


def _keys(title: str, artist: str | None) -> tuple[str, str]:
    """Cache keys: normalised so upload variants share one row."""
    return match.song_key(title or ""), (artist or "").lower()


def resolve(
    conn: Any, title: str, artist: str | None = None, sleep: Callable[[float], None] = time.sleep
) -> dict[str, Any] | None:
    """Cached title+artist -> Deezer identity. None when nothing matches.

    Negative results are cached too: a song Deezer genuinely does not carry
    would otherwise cost two searches on every pass forever.
    """
    song_key, artist_key = _keys(title, artist)
    if not song_key:
        return None

    cached = graph_store.get_resolution(conn, song_key, artist_key)
    if cached is not None:
        if cached["status"] != graph_store.STATUS_OK:
            return None
        return {
            "id": cached["track_id"],
            "title": cached["title"],
            "artist_name": cached["artist_name"],
            "artist_id": cached["artist_id"],
        }

    # Only credit-matched rows are real identity -- see search_tracks.
    hits = [h for h in search_tracks(title, artist, sleep=sleep) if h.get("matched")]
    best = hits[0] if hits else None
    graph_store.put_resolution(
        conn,
        song_key,
        artist_key,
        track_id=best["id"] if best else None,
        artist_id=best["artist_id"] if best else None,
        title=best["title"] if best else None,
        artist_name=best["artist_name"] if best else None,
        status=graph_store.STATUS_OK if best else graph_store.STATUS_NO_MATCH,
    )
    return best


def resolve_artist(
    conn: Any, artist: str, sleep: Callable[[float], None] = time.sleep
) -> dict[str, Any] | None:
    """Deezer artist id for a name, for seeds known only by artist."""
    if not artist:
        return None
    sleep(THROTTLE)
    encoded = urllib.parse.quote(artist.strip()[:180])
    for hit in _data(_get_safe(f"{API}/search/artist?q={encoded}&limit=5")):
        if match.artist_matches(hit.get("name"), artist):
            return {"id": hit.get("id"), "name": hit.get("name")}
    return None


# --- adjacency --------------------------------------------------------------


def related_artists(
    conn: Any, artist_id: int, sleep: Callable[[float], None] = time.sleep
) -> list[dict[str, Any]]:
    """Artists adjacent to this one. Cached, including the empty answer.

    This is the signal that stands in for the per-track radio Deezer does not
    have. It is the strongest thing the graph offers, and on this library's
    Punjabi/Bollywood catalogue it is markedly better than anything the
    providers expose.
    """
    if not artist_id:
        return []
    if graph_store.was_fetched(conn, _EP_RELATED, artist_id):
        return graph_store.get_related_artists(conn, artist_id)

    sleep(THROTTLE)
    rows = [
        {"id": a.get("id"), "name": a.get("name")}
        for a in _data(_get_safe(f"{API}/artist/{artist_id}/related"))
        if a.get("id")
    ]
    graph_store.put_related_artists(conn, artist_id, rows)
    graph_store.record_fetch(conn, _EP_RELATED, artist_id, graph_store.STATUS_OK, len(rows))
    return rows


def artist_tracks(
    conn: Any,
    artist_id: int,
    kind: str = KIND_TOP,
    limit: int = 25,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """An artist's tracks -- `KIND_TOP` (ranked, stable) or `KIND_RADIO`.

    Radio is treated as best-effort and kept under a separate `kind` rather
    than pooled with top tracks. PLAN.md recorded `/artist/{id}/radio` as empty
    for AP Dhillon -- exactly the kind of artist this library is full of --
    while a re-probe on 2026-08-24 returned 25 tracks for that same artist. One
    sample either way is not a measurement, so the two stay separable until
    `scripts/quality_check.py` settles it.
    """
    if not artist_id:
        return []
    endpoint = _EP_RADIO if kind == KIND_RADIO else _EP_TOP
    if graph_store.was_fetched(conn, endpoint, artist_id):
        return graph_store.get_artist_tracks(conn, artist_id, kind)

    sleep(THROTTLE)
    path = "radio" if kind == KIND_RADIO else f"top?limit={limit}"
    rows = [_track_row(t) for t in _data(_get_safe(f"{API}/artist/{artist_id}/{path}")) if t.get("id")]
    graph_store.put_artist_tracks(conn, artist_id, kind, rows)
    graph_store.record_fetch(conn, endpoint, artist_id, graph_store.STATUS_OK, len(rows))
    return rows


# --- playlists (the provider-neutral mood atlas) ----------------------------


def search_playlists(query: str, limit: int = 25, sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """Find playlists by text. Deezer permits this AND reading their tracks --
    exactly what Spotify forbids, and what makes a neutral mood atlas possible."""
    if not query.strip():
        return []
    sleep(THROTTLE)
    encoded = urllib.parse.quote(query.strip()[:180])
    return [
        {"id": p.get("id"), "title": p.get("title"), "track_count": p.get("nb_tracks")}
        for p in _data(_get_safe(f"{API}/search/playlist?q={encoded}&limit={limit}"))
        if p.get("id")
    ]


def playlist_tracks(playlist_id: int, limit: int = 100, sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """Read a playlist's tracks."""
    sleep(THROTTLE)
    rows = _data(_get_safe(f"{API}/playlist/{playlist_id}/tracks?limit={limit}"))
    return [_track_row(t) for t in rows if t.get("id")]


# --- candidate generation ---------------------------------------------------


def neighbours(
    conn: Any,
    seed: dict[str, Any],
    *,
    related_to_expand: int = 3,
    per_artist: int = 10,
    include_radio: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Graph candidates for one resolved seed track.

    Artist-centric by necessity (no track radio exists): the seed's own artist,
    then its adjacent artists' catalogues. Each candidate is tagged with the
    graph source that surfaced it, so `signals._merge_and_score` can rank on
    agreement between graph and native signals exactly as it already does
    between native ones.

    Returns graph-shaped candidates -- **no provider id**. They carry
    title/artist text only, and are resolved back to provider ids lazily, after
    ranking, by `signals.resolve_candidates`. Resolving a 500-candidate pool
    eagerly (one provider search each) would be absurd.
    """
    artist_id = seed.get("artist_id")
    if not artist_id:
        return []

    out: list[dict[str, Any]] = []
    seed_track_id = seed.get("id")

    def add(rows: list[dict[str, Any]], source: str) -> None:
        for row in rows[:per_artist]:
            if not row.get("id") or row["id"] == seed_track_id:
                continue
            out.append({**row, "source": source})

    add(artist_tracks(conn, artist_id, KIND_TOP, sleep=sleep), "graph_artist")
    if include_radio:
        add(artist_tracks(conn, artist_id, KIND_RADIO, sleep=sleep), "graph_radio")

    for rel in related_artists(conn, artist_id, sleep=sleep)[:related_to_expand]:
        if rel.get("id"):
            add(artist_tracks(conn, rel["id"], KIND_TOP, sleep=sleep), "graph_related")

    return out
