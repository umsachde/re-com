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

**That rejection has since been narrowed, not reversed -- see `brainz.py`.**
It tested `similar-recordings`, a *track*-level endpoint, which this module's
own "there is no track-level radio" note below should have flagged as the wrong
shape to ask for: graph similarity here is artist-centric. The labs API's
`similar-artists` was re-probed on 2026-09-11 and is populated, sub-second, and
independent of Deezer (Jaccard 0.137), so it now runs alongside this source as
`graph_related_lb`. Deezer remains the graph's *catalogue* -- ListenBrainz
returns artists and never tracks, so every neighbour it finds still crosses
back into `artist_tracks` here.

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


# Deezer's "no data" error code: a real, cacheable miss.
_DEEZER_NO_DATA = 800


def _rows(url: str) -> list[dict[str, Any]] | None:
    """Rows for a cached lookup, or None when Deezer could not be asked.

    Every cache in this module stores negative answers on purpose, so the one
    thing a caller must never do is cache a *failure* as one. Deezer signals
    its quota as a 200 with an error body (code 4), not an HTTP error, so a
    burst of lookups -- several seeds at once, each resolving neighbours --
    used to write "not on Deezer" permanently for songs that are. Only code
    800 and an empty `data` list mean nothing is there.
    """
    try:
        payload = _get(url)
    except _NET_ERRORS:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        return [] if code == _DEEZER_NO_DATA else None
    rows = payload.get("data")
    return rows if isinstance(rows, list) else None


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
    return _search_tracks(title, artist, sleep)[0]


def _search_tracks(
    title: str, artist: str | None, sleep: Callable[[float], None]
) -> tuple[list[dict[str, Any]], bool]:
    """`search_tracks` plus whether every search it needed actually got an answer."""
    if not title:
        return [], True

    complete = True
    hits: list[dict[str, Any]] = []
    if artist:
        found = _search(f"{title} {artist}".strip(), sleep)
        complete = found is not None
        hits = found or []
    matching = [h for h in hits if match.artist_matches((h.get("artist") or {}).get("name", ""), artist)]

    if not matching:
        fallback = _search(title, sleep)
        complete = complete and fallback is not None
        for hit in fallback or []:
            if match.same_title(hit.get("title"), title):
                matching.append(hit)

    rows = [{**_track_row(h), "matched": True} for h in matching]
    if not matching and hits:
        rows.append({**_track_row(hits[0]), "matched": False})
    return rows, complete


def _search(query: str, sleep: Callable[[float], None]) -> list[dict[str, Any]] | None:
    if not query.strip():
        return []
    encoded = urllib.parse.quote(query.strip()[:180])
    return _rows(f"{API}/search?q={encoded}&limit={MAX_CANDIDATES}")


def track_detail(track_id: int, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any] | None:
    """Full track record, including the `bpm` field `tempo.py` wants."""
    sleep(THROTTLE)
    detail = _get_safe(f"{API}/track/{track_id}")
    # Deezer reports quota and other errors as a 200 with an error body.
    return detail if isinstance(detail, dict) and "error" not in detail else None


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
    rows, complete = _search_tracks(title, artist, sleep)
    hits = [h for h in rows if h.get("matched")]
    best = hits[0] if hits else None
    if not complete:
        # A partial answer is neither a miss nor settled identity: the failed
        # artist-qualified search might have matched better than a title-only one.
        return best
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
    """Cached Deezer artist id for a name, for seeds known only by artist.

    Negative results are cached alongside the positive ones, as everywhere else
    in this module. The cache stopped being optional when ListenBrainz became
    the second source (`brainz.py`): every LB neighbour arrives as a *name* and
    must cross back into Deezer's catalogue to become tracks, so an uncached
    lookup here is one search per neighbour per seed, on every call, forever.
    """
    if not artist or not artist.strip():
        return None
    artist_key = artist.strip().lower()

    cached = graph_store.get_artist_lookup(conn, artist_key)
    if cached is not None:
        if cached["status"] != graph_store.STATUS_OK:
            return None
        return {"id": cached["artist_id"], "name": cached["name"]}

    sleep(THROTTLE)
    encoded = urllib.parse.quote(artist.strip()[:180])
    found = _rows(f"{API}/search/artist?q={encoded}&limit=5")
    if found is None:
        return None
    best = next((h for h in found if match.artist_matches(h.get("name"), artist)), None)
    graph_store.put_artist_lookup(
        conn,
        artist_key,
        artist_id=best.get("id") if best else None,
        name=best.get("name") if best else None,
        status=graph_store.STATUS_OK if best else graph_store.STATUS_NO_MATCH,
    )
    return {"id": best["id"], "name": best.get("name")} if best else None


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
    found = _rows(f"{API}/artist/{artist_id}/related")
    if found is None:
        return []
    rows = [{"id": a.get("id"), "name": a.get("name")} for a in found if a.get("id")]
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
    found = _rows(f"{API}/artist/{artist_id}/{path}")
    if found is None:
        return []
    rows = [_track_row(t) for t in found if t.get("id")]
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


def _brainz_related_ids(
    conn: Any,
    artist_name: str | None,
    want: int,
    seen: set[int],
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """ListenBrainz neighbours of `artist_name`, as Deezer artist ids.

    Kept behind its own try/except because the second source is a *supplement*:
    the measurement that justified it (`brainz.py`'s header) also found its
    coverage partial -- Dua Lipa has no ListenBrainz neighbours at all. A seed
    it cannot answer for must degrade to Deezer-only silently, never fail.

    `seen` carries the Deezer ids already expanded so the two sources' overlap
    (Jaccard 0.137, so real but small) is not crawled twice.
    """
    if not artist_name:
        return []
    try:
        import brainz  # local: keeps the second source optional at import time

        names = brainz.related_artist_names(conn, artist_name, limit=want * 3, sleep=sleep)
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for name in names:
        if len(out) >= want:
            break
        resolved = resolve_artist(conn, name, sleep=sleep)
        if resolved and resolved.get("id") and resolved["id"] not in seen:
            seen.add(resolved["id"])
            out.append(resolved)
    return out


def _similar_tracks(
    conn: Any,
    source: str,
    title: str | None,
    artist_name: str | None,
    want: int,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """Track-level neighbours from `brainz` or `lastfm`, resolved to Deezer track rows.

    Both modules expose the same `similar_tracks`. The same supplement contract
    as `_brainz_related_ids`: no credential, no coverage or a failure all
    degrade to nothing, never an error.
    """
    if not title or not artist_name:
        return []
    try:
        import importlib

        module = importlib.import_module(source)  # local: keeps each source optional at import time
        similar = module.similar_tracks(conn, title, artist_name, limit=want, sleep=sleep)
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for rec in similar:
        try:
            resolved = resolve(conn, rec["title"], rec.get("artist_name"), sleep=sleep)
        except Exception:
            continue
        if resolved and resolved.get("id"):
            out.append(resolved)
    return out


def neighbours(
    conn: Any,
    seed: dict[str, Any],
    *,
    related_to_expand: int = 3,
    per_artist: int = 10,
    include_radio: bool = True,
    brainz_to_expand: int = 3,
    brainz_tracks: int = 10,
    lastfm_tracks: int = 10,
    brainz_artist: str | None = None,
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

    expanded: set[int] = {artist_id}
    for rel in related_artists(conn, artist_id, sleep=sleep)[:related_to_expand]:
        if rel.get("id"):
            expanded.add(rel["id"])
            add(artist_tracks(conn, rel["id"], KIND_TOP, sleep=sleep), "graph_related")

    # The second source (PLAN.md 7.3). Tagged distinctly so a track both
    # sources surface lands in one candidate with two `sources` entries, which
    # is what `signals._merge_and_score` already reads as agreement -- the
    # single-signal problem §7.2 measured is fixed by the tag, not by new
    # scoring. ListenBrainz supplies adjacency only; the tracks still come from
    # Deezer's catalogue, so this narrows the single-point-of-failure rather
    # than removing it.
    #
    # `brainz_artist` is the *provider's* credit, and preferring it over the
    # Deezer-resolved name is not a nicety. Deezer credits "Kesariya" to
    # Pritam, its composer, while YouTube and Spotify credit Arijit Singh, who
    # sings it. Looking up the composer's neighbours returned a set with
    # nothing to do with the seed -- caught on the first live run. Indian film
    # music makes the composer-vs-performer split the common case, not an edge
    # one, so the credit the user actually listened under is the right seed.
    lb_seed = brainz_artist or seed.get("artist_name")
    for rel in _brainz_related_ids(conn, lb_seed, brainz_to_expand, expanded, sleep):
        add(artist_tracks(conn, rel["id"], KIND_TOP, sleep=sleep), "graph_related_lb")

    # Track-level similarity (PLAN.md 7.12): the only signal here that tells two
    # songs by one artist apart. Deezer's title is the cleaner query; the
    # provider's credit is the right artist, for the composer reason above.
    for row in _similar_tracks(conn, "brainz", seed.get("title"), lb_seed, brainz_tracks, sleep):
        if row["id"] != seed_track_id:
            out.append({**row, "source": "graph_similar_lb"})

    # last.fm reaches the Punjabi catalogue ListenBrainz cannot (PLAN.md 7.13).
    lfm_rows = _similar_tracks(conn, "lastfm", seed.get("title"), lb_seed, lastfm_tracks, sleep)
    composer = seed.get("artist_name")
    if not lfm_rows and composer and composer != lb_seed:
        # Unlike ListenBrainz's artist adjacency, last.fm's track-level listener
        # data is split by *credit*, not just spelling: "Channa Mereya" has 983
        # last.fm listeners under Arijit Singh (performer) and 52,899 under
        # Pritam (composer) -- a different track entry, not a dupe of the same
        # one. Retried only on a miss, and cached like any other lookup, so the
        # cost is one extra call per seed, once ever (PLAN.md 7.13 residual).
        lfm_rows = _similar_tracks(conn, "lastfm", seed.get("title"), composer, lastfm_tracks, sleep)
    for row in lfm_rows:
        if row["id"] != seed_track_id:
            out.append({**row, "source": "graph_similar_lfm"})

    return out
