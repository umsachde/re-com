"""Mood-driven recommendation.

The obvious way to build this is to run v1 and then filter the results by mood.
Don't: filtering a Daft Punk radio for "melancholy" returns the least danceable
Daft-Punk-adjacent tracks, not melancholy music. The mood has to decide *where
candidates come from*, not merely which survive.

So the flow is: read the mood, pick seeds from the listener's own library that
already sit near it, run v1's proven three-signal generation from those seeds,
add songs from mood playlists near the target, then rank on how many signals
agreed *and* how well each song fits the mood, and finally shape the result
into a sequence that moves (see arc.py).
"""

import math
from typing import Any, Iterable

import arc as arc_module
import filters
import label
import match
import moodspace
import sense
import signals
import store

SEED_COUNT = 6
CANDIDATES_PER_SLOT = 12

# Seeding from a playlist reads every track in it, but each seed costs ~4 API
# calls -- a 100-song playlist would fire ~400. So every track is *considered*
# (scored for mood fit, kept only if it's a genuine match) and the best of them
# are what actually seed the search.
PLAYLIST_SEED_CAP = 20

# How much of a requested count is allowed to be filler (unrated, or rated but
# a poor fit) rather than a genuine mood match. Asking for 100 and getting 7
# real matches shouldn't come back as 100 -- it should come back as 7 plus a
# bounded amount of the best-available filler, honestly labelled as such.
FLUFF_CAP_RATIO = 0.25
ATLAS_NEIGHBOUR_LIMIT = 400

# Bounding box for the SQL pre-filter before exact distances are computed.
_VALENCE_WINDOW = 0.4
_ENERGY_WINDOW = 0.3

# A song by an artist already in the library is a safer bet at the same mood
# fit -- taste is not uniform across a genre.
KNOWN_ARTIST_BOOST = 1.15
ATLAS_SOURCE = "mood-playlist"

# How far feedback on past recommendations is allowed to move an artist's
# candidates. Bounded tightly and on purpose: most of this evidence is
# *inferred* (store.infer_implicit_feedback), and signal agreement between
# independent discovery sources is the thing this engine actually trusts. A
# learned preference should break ties, not overrule retrieval.
AFFINITY_MAX_BOOST = 1.25
AFFINITY_MAX_PENALTY = 0.75
# Net reactions at which the multiplier saturates. Low, because the counts are
# small -- but it means one stray reaction can't swing an artist to the extreme.
AFFINITY_SATURATION = 3

# Free-text mood words. Claude is expected to pass a precise `vector` for
# anything nuanced; this exists so the tool still works when it doesn't, and
# for words the anchor names don't cover.
_FEELING_WORDS: dict[str, str | dict[str, float]] = {
    "sad": "Sad", "down": "Sad", "low": "Sad", "blue": "Sad", "depressed": "Sad",
    "heartbroken": "Sad", "heartbreak": "Sad", "lonely": "Sad", "grieving": "Sad",
    "miserable": "Sad", "crying": "Sad", "hurt": "Sad",
    "chill": "Chill", "relaxed": "Chill", "mellow": "Chill", "calm": "Chill",
    "laidback": "Chill", "easy": "Chill", "cruising": "Chill",
    "sleepy": "Sleep", "tired": "Sleep", "exhausted": "Sleep", "bedtime": "Sleep",
    "focus": "Focus", "focused": "Focus", "working": "Focus", "studying": "Focus",
    "concentrate": "Focus", "productive": "Focus", "coding": "Focus",
    "commute": "Commute", "driving": "Commute", "drive": "Commute", "road": "Commute",
    "happy": "Feel good", "good": "Feel good", "great": "Feel good", "joyful": "Feel good",
    "cheerful": "Feel good", "sunny": "Feel good", "upbeat": "Feel good",
    "romantic": "Romance", "love": "Romance", "tender": "Romance", "intimate": "Romance",
    "hyped": "Energize", "energized": "Energize", "pumped": "Energize", "amped": "Energize",
    "excited": "Energize",
    "workout": "Workout", "gym": "Workout", "lifting": "Workout", "running": "Workout",
    "training": "Workout",
    "party": "Party", "partying": "Party", "celebrating": "Party",
    "gaming": "Gaming",
    "angry": {"valence": -0.45, "energy": 0.85, "tension": 0.9, "depth": 0.45},
    "furious": {"valence": -0.55, "energy": 0.9, "tension": 0.95, "depth": 0.4},
    "rage": {"valence": -0.5, "energy": 0.9, "tension": 0.95, "depth": 0.35},
    "frustrated": {"valence": -0.4, "energy": 0.6, "tension": 0.8, "depth": 0.5},
    "nostalgic": {"valence": -0.05, "energy": 0.35, "tension": 0.2, "depth": 0.85},
    "wistful": {"valence": -0.2, "energy": 0.25, "tension": 0.2, "depth": 0.85},
    "bittersweet": {"valence": -0.15, "energy": 0.35, "tension": 0.25, "depth": 0.8},
    "anxious": {"valence": -0.4, "energy": 0.5, "tension": 0.85, "depth": 0.6},
    "stressed": {"valence": -0.35, "energy": 0.55, "tension": 0.85, "depth": 0.45},
    "overwhelmed": {"valence": -0.45, "energy": 0.45, "tension": 0.8, "depth": 0.6},
    "reflective": {"valence": 0.0, "energy": 0.2, "tension": 0.15, "depth": 0.9},
    "introspective": {"valence": -0.1, "energy": 0.2, "tension": 0.2, "depth": 0.9},
    "thoughtful": {"valence": 0.05, "energy": 0.25, "tension": 0.15, "depth": 0.85},
    "confident": {"valence": 0.6, "energy": 0.75, "tension": 0.45, "depth": 0.4},
    "powerful": {"valence": 0.5, "energy": 0.85, "tension": 0.6, "depth": 0.35},
    "restless": {"valence": -0.1, "energy": 0.7, "tension": 0.7, "depth": 0.45},
    "bored": {"valence": -0.15, "energy": 0.3, "tension": 0.35, "depth": 0.3},
}


def parse_feeling(text: str | None) -> dict[str, float] | None:
    """Map free text to a mood vector by matching known feeling words.

    Deliberately simple. The tool's `vector` argument is the precise path --
    Claude reads the sentence far better than a word list can, and "wistful but
    still wants to get things done" has no keyword.
    """
    if not text:
        return None
    words = "".join(c.lower() if (c.isalnum() or c.isspace()) else " " for c in text).split()
    hits = []
    for word in words:
        found = _FEELING_WORDS.get(word)
        if found is None:
            continue
        hits.append(moodspace.ANCHORS[found] if isinstance(found, str) else moodspace.vector(**found))
    if not hits:
        return None
    return moodspace.blend((vec, 1.0) for vec in hits)


def resolve_target(
    conn: Any,
    yt: Any | None = None,
    feeling: str | None = None,
    vector: dict[str, float] | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    """Work out what mood to aim at, and be explicit about where it came from.

    Priority is what the listener said, then what they've been playing, then a
    weak time-of-day prior. Never let the clock overrule a person.
    """
    if vector is not None and moodspace.is_vector(vector):
        return {"target": moodspace.clamp(vector), "origin": "explicit", "evidence": []}

    if context and context.title() in moodspace.ANCHORS:
        return {
            "target": dict(moodspace.ANCHORS[context.title()]),
            "origin": "context",
            "evidence": [f"Using the {context.title()} profile."],
        }

    parsed = parse_feeling(feeling)
    if parsed is not None:
        return {"target": parsed, "origin": "feeling", "evidence": [f"Read from your words: {moodspace.describe(parsed)}."]}

    read = sense.read_mood(conn, yt)
    if read["vector"] is not None:
        return {"target": read["vector"], "origin": "history", "evidence": read["evidence"], "confidence": read["confidence"]}

    prior = sense.time_of_day_prior()
    return {
        "target": moodspace.vector(valence=0.15, energy=0.5 + prior["energy_bias"], tension=0.3, depth=0.4 + prior["depth_bias"]),
        "origin": "time-of-day",
        "evidence": [f"No mood given and nothing readable in your history — defaulting to a {prior['label']} profile."],
    }


def pick_seeds(conn: Any, target: dict[str, float], count: int = SEED_COUNT, genres: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Choose songs from the listener's own library that already sit near the mood.

    This is the heart of the whole design. "Something nostalgic" should branch
    out from the songs *this person* finds nostalgic, which is knowable only
    because their library has been labelled.
    """
    library = store.library_video_ids(conn)
    if not library:
        return []

    moods = label.resolve_many(conn, library)
    if not moods:
        return []

    genre_filter = {g.lower() for g in genres} if genres else None

    scored = []
    for video_id, entry in moods.items():
        track = store.get_track(conn, video_id) or {}
        if genre_filter:
            genre = label.genre_prior(conn, video_id)
            if not genre or genre.lower() not in genre_filter:
                continue
        scored.append(
            {
                "videoId": video_id,
                "title": track.get("title"),
                "artists": track.get("artists"),
                "fit": moodspace.fit(entry["vector"], target),
                "seed_score": moodspace.seed_score(entry["vector"], target, entry["confidence"]),
                "mood": entry["vector"],
            }
        )

    scored.sort(key=lambda s: -s["seed_score"])

    # Spread seeds across artists: six songs by one artist is a radio station,
    # not a mood.
    picked, seen_artists = [], set()
    for entry in scored:
        artist = label.primary_artist(entry["artists"])
        if artist and artist in seen_artists:
            continue
        if artist:
            seen_artists.add(artist)
        picked.append(entry)
        if len(picked) >= count:
            break
    return picked


def pick_seeds_from_playlist(
    conn: Any,
    tracks: list[dict[str, Any]],
    target: dict[str, float],
    cap: int = PLAYLIST_SEED_CAP,
) -> dict[str, Any]:
    """Choose seeds from one playlist's own tracks, not the whole library.

    pick_seeds() answers "what in this person's library fits this mood".
    This answers a narrower question: "what in *this playlist* fits this mood",
    for when someone points at a playlist and a feeling together.

    Every track is considered, but only genuine matches seed the search -- a
    track whose own mood can't be resolved, or which fits the target no better
    than an unlabelled song is assumed to, would send the radio somewhere the
    listener didn't ask for. On an off-mood playlist that legitimately yields
    few seeds (or none), which the caller reports rather than papering over.
    """
    store.upsert_tracks(conn, tracks)
    ids = [t["videoId"] for t in tracks if t.get("videoId")]
    moods = label.resolve_or_derive(conn, ids)

    scored = []
    for video_id, entry in moods.items():
        fit = moodspace.fit(entry["vector"], target)
        if fit <= arc_module.UNRATED_FIT:
            continue
        # Read identity back from the store, which normalised it on upsert --
        # the raw API hands artists back in several shapes.
        track = store.get_track(conn, video_id) or {}
        scored.append(
            {
                "videoId": video_id,
                "title": track.get("title"),
                "artists": track.get("artists"),
                "fit": fit,
                "seed_score": moodspace.seed_score(entry["vector"], target, entry["confidence"]),
                "mood": entry["vector"],
            }
        )

    scored.sort(key=lambda s: -s["seed_score"])

    # Same reasoning as pick_seeds: spread across artists so the seeds describe
    # a mood rather than one artist's back catalogue.
    picked, seen_artists = [], set()
    for entry in scored:
        artist = label.primary_artist(entry["artists"])
        if artist and artist in seen_artists:
            continue
        if artist:
            seen_artists.add(artist)
        picked.append(entry)
        if len(picked) >= cap:
            break

    return {
        "seeds": picked,
        "considered": len(ids),
        "genuine": len(scored),
        "capped": max(0, len(scored) - len(picked)),
    }


def atlas_neighbours(conn: Any, target: dict[str, float], limit: int = ATLAS_NEIGHBOUR_LIMIT) -> list[dict[str, Any]]:
    """Songs from YouTube's mood playlists that sit near the target.

    The fourth signal, and the only one that reaches outside the listener's own
    taste graph -- radio seeded from their library can only ever circle it.
    """
    rows = conn.execute(
        "SELECT tm.video_id, tm.valence, tm.energy, tm.tension, tm.depth, tm.confidence, "
        "       t.title, t.artists, t.album "
        "FROM track_mood tm LEFT JOIN track t USING (video_id) "
        "WHERE tm.source = 'atlas' "
        "  AND tm.valence BETWEEN ? AND ? AND tm.energy BETWEEN ? AND ?",
        (
            target["valence"] - _VALENCE_WINDOW, target["valence"] + _VALENCE_WINDOW,
            target["energy"] - _ENERGY_WINDOW, target["energy"] + _ENERGY_WINDOW,
        ),
    ).fetchall()

    scored = []
    for row in rows:
        vector = moodspace.vector(
            valence=row["valence"], energy=row["energy"], tension=row["tension"], depth=row["depth"]
        )
        scored.append(
            {
                "videoId": row["video_id"],
                "title": row["title"],
                "artists": [a for a in (row["artists"] or "").split(" & ") if a],
                "album": row["album"],
                "mood": vector,
                "fit": moodspace.fit(vector, target),
                "sources": {ATLAS_SOURCE},
                "score": 1,
            }
        )
    scored.sort(key=lambda c: -c["fit"])
    return scored[:limit]


def artist_affinity(conn: Any) -> dict[str, float]:
    """A per-artist ranking multiplier learned from how past picks landed.

    Feedback arrives per *song*, but its useful signal is per *artist*: a song
    that was played is usually then liked, at which point the library exclusion
    means it can never be recommended again anyway. What survives is the
    direction it pointed in -- this artist's neighbourhood is worth more of.

    Returns only artists with a non-zero net, so the common case (no feedback
    yet) is an empty dict and the ranking below is unchanged.
    """
    counts = store.feedback_counts(conn)
    if not counts:
        return {}

    net: dict[str, int] = {}
    for video_id, tally in counts.items():
        track = store.get_track(conn, video_id) or {}
        artist = label.primary_artist(track.get("artists"))
        if not artist:
            continue
        net[artist] = net.get(artist, 0) + tally["positive"] - tally["negative"]

    affinity: dict[str, float] = {}
    for artist, score in net.items():
        if not score:
            continue
        strength = min(abs(score), AFFINITY_SATURATION) / AFFINITY_SATURATION
        if score > 0:
            affinity[artist] = 1.0 + (AFFINITY_MAX_BOOST - 1.0) * strength
        else:
            affinity[artist] = 1.0 - (1.0 - AFFINITY_MAX_PENALTY) * strength
    return affinity


def _library_artists(conn: Any) -> set[str]:
    return {
        label.primary_artist(r["artists"])
        for r in conn.execute(
            "SELECT DISTINCT t.artists FROM track t JOIN library_track l USING (video_id)"
        )
        if label.primary_artist(r["artists"])
    }


def bridge_expand(
    yt: Any,
    conn: Any,
    survivors: list[dict[str, Any]],
    exclude: set[str],
    want: Iterable[str] | None,
    exclude_languages: Iterable[str] | None,
    allow_unlabelled: bool,
    needed: int,
    max_bridges: int = 3,
    hops: int = 1,
) -> tuple[list[dict[str, Any]], int]:
    """Grow a language-filtered result by re-seeding from the songs that passed.

    Filtering a pool for a language the seed isn't in can only ever return the
    few matching songs that happened to be there -- seeding from a Punjabi
    track and asking for English left 3 results out of 8 requested. The fix is
    the same one the mood engine needed: change retrieval, don't just filter.

    The survivors are English songs already established as similar to the seed,
    so radio from them reaches more English songs in the same neighbourhood.
    One hop by default, because each is another round of API calls and the
    results drift further from the original seed with every jump.
    """
    import filters

    if not survivors or len(survivors) >= needed:
        return survivors, 0

    found = {s["videoId"]: s for s in survivors}
    added = 0

    for _ in range(hops):
        if len(found) >= needed:
            break
        bridges = sorted(found.values(), key=lambda c: -c.get("base_score", 0))[:max_bridges]

        per_seed = signals.gather_seeds(yt, [bridge["videoId"] for bridge in bridges])
        if not per_seed:
            break

        merged = signals._merge_and_score(per_seed)
        fresh = [
            {
                "videoId": vid, "title": c["title"], "artists": c["artists"],
                "album": c.get("album"), "sources": sorted(c["sources"]),
                "score": c["score"], "base_score": c["score"],
            }
            for vid, c in merged.items()
            if vid not in exclude and vid not in found
        ]

        kept, _ = filters.apply_language(
            conn, fresh, want=want, exclude=exclude_languages, allow_unlabelled=allow_unlabelled
        )
        for candidate in kept:
            if candidate["videoId"] not in found:
                found[candidate["videoId"]] = candidate
                added += 1

    return list(found.values()), added


def _language_note(report: dict[str, Any], limit: int) -> str:
    """Say plainly what a language filter removed. Dropping songs for lack of a
    label is a real cost and must not be invisible."""
    wanted = ", ".join(report["want"] or []) or f"not {', '.join(report['exclude'] or [])}"
    parts = [f"Language filter ({wanted}): kept {report['kept']}"]
    if report["dropped_wrong_language"]:
        parts.append(f"dropped {report['dropped_wrong_language']} in other languages")
    if report["dropped_unlabelled"]:
        parts.append(
            f"dropped {report['dropped_unlabelled']} with no language label "
            "(pass allow_unlabelled_language=True to keep them)"
        )
    note = "; ".join(parts) + "."
    if report["kept"] < limit:
        note += (
            f" That leaves fewer than the {limit} requested — run scripts/build_genres.py "
            "to label more artists."
        )
    return note


def _tempo_note(report: dict[str, Any]) -> str:
    parts = []
    if report["target_bpm"]:
        parts.append(f"Tempo target {report['target_bpm']:.0f}bpm")
    if report["range"]:
        low, high = report["range"]
        parts.append(f"Tempo range {low or '-'}-{high or '-'}bpm")
    parts.append(f"{report['with_known_tempo']} candidates had a known BPM")
    if report["dropped_out_of_range"]:
        parts.append(f"{report['dropped_out_of_range']} dropped as out of range")
    if report["unknown_tempo_kept"]:
        parts.append(
            f"{report['unknown_tempo_kept']} kept with unknown BPM (Deezer has no tempo for "
            "much of the non-English catalogue, so these are not dropped)"
        )
    return "; ".join(parts) + "."


def _is_genuine_match(song: dict[str, Any]) -> bool:
    """A real mood label whose fit actually clears the unrated baseline --
    as opposed to unrated filler, or a rated song that's a poor fit anyway."""
    return bool(song["rated"]) and song["mood_fit"] is not None and song["mood_fit"] > arc_module.UNRATED_FIT


# How many times to re-sequence after music-graph candidates fail to resolve.
# Each round costs only the searches for candidates newly promoted into the
# sequence, and three is enough for the drop rates measured (1 in 10).
_RESOLVE_ROUNDS = 3


def _sequence_and_resolve(
    yt: Any,
    shortlist: list[dict[str, Any]],
    slot_targets: list[dict[str, float]],
    blocked: set[str],
    has_graph: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Sequence the arc, then give the chosen songs provider ids.

    **Stage two of exclusion, and the only place a provider search happens.**
    Resolving after sequencing rather than before ranking is the same lazy
    discipline v6 measured into `resolve_candidates`: the sequencer has already
    chosen exactly the songs that will be returned, so this is the smallest set
    that needs ids -- and zero searches happen when native signals filled the
    response, which is what keeps YouTube's latency where it was.

    **Why it loops.** A candidate that fails to resolve leaves its arc slot
    empty, and simply appending a replacement would put an unslotted song at
    the end of a curve the user asked to be shaped. Dropping the dead
    candidates and re-sequencing instead lets the next-best song claim that
    slot properly. Measured: a 10-song YouTube request lost one candidate at
    resolution and came back with 9 before this.
    """
    def ref_of(candidate: dict[str, Any]) -> int | None:
        return (candidate.get("graphRef") or {}).get("trackId")

    dead: set[int] = set()
    total_unresolved = 0
    ordered: list[dict[str, Any]] = []

    for _ in range(_RESOLVE_ROUNDS):
        pool = [c for c in shortlist if ref_of(c) not in dead]
        ordered = arc_module.sequence(pool, slot_targets)
        # Nothing to resolve: no graph in play, or the sequence is all native.
        if not has_graph or all(c.get("videoId") for c in ordered):
            return ordered, total_unresolved

        resolved, unresolved = signals.resolve_candidates(
            yt, ordered, len(ordered), blocked
        )
        total_unresolved += unresolved
        if not unresolved or len(resolved) >= len(slot_targets):
            return resolved, total_unresolved

        # `resolve_candidates` works in place and pops `graphRef` off the ones
        # it resolved, so whatever still carries a reference is exactly what
        # failed -- and must not be offered the slot again next round.
        dead |= {ref_of(c) for c in ordered if ref_of(c) is not None}
        ordered = resolved

    return ordered, total_unresolved


def _cap_fluff(ordered: list[dict[str, Any]], requested: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Bound how much of the sequenced list is filler.

    arc.sequence() fills every slot it can from whatever's left in the pool,
    with no floor on quality -- asking for 100 with 7 real matches otherwise
    comes back as 100, the other 93 being progressively worse guesses. Genuine
    matches are always kept, in their arc order; filler is kept only up to
    ceil(requested * FLUFF_CAP_RATIO), also in arc order, and the rest dropped.
    """
    fluff_cap = math.ceil(requested * FLUFF_CAP_RATIO)
    kept: list[dict[str, Any]] = []
    genuine = fluff_used = 0
    for song in ordered:
        if _is_genuine_match(song):
            genuine += 1
            kept.append(song)
        elif fluff_used < fluff_cap:
            fluff_used += 1
            kept.append(song)

    for slot, song in enumerate(kept):
        song["slot"] = slot

    return kept, {
        "genuine": genuine, "requested": requested,
        "fluff_cap": fluff_cap, "fluff_used": fluff_used,
    }


def _graph_moods(graph_conn: Any, pool: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Moods for graph candidates, read straight out of the graph atlas.

    Keyed by the pool key (`graph:<deezer id>`), so it merges into the same
    dict `label.resolve_or_derive` returns and nothing downstream needs to know
    a candidate came from the graph. Reported as source `graph_atlas`, the same
    name the propagated library labels carry, so `SOURCE_PRIORITY` and every
    coverage report keep meaning one thing.
    """
    if graph_conn is None:
        return {}

    import graph_atlas  # local: keeps the graph optional at import time

    out: dict[str, dict[str, Any]] = {}
    for key, candidate in pool.items():
        ref = candidate.get("graphRef")
        if not ref or not ref.get("trackId"):
            continue
        mood = graph_atlas.get_mood(graph_conn, ref["trackId"])
        if not mood:
            continue
        out[key] = {
            "vector": moodspace.vector(
                valence=mood["valence"], energy=mood["energy"],
                tension=mood["tension"], depth=mood["depth"],
            ),
            "source": graph_atlas.SOURCE,
            "confidence": mood["confidence"] if mood["confidence"] is not None else 0.5,
        }
    return out


def build(
    yt: Any,
    conn: Any,
    exclude: set[str],
    feeling: str | None = None,
    vector: dict[str, float] | None = None,
    context: str | None = None,
    arc: str = "mirror",
    limit: int = 20,
    genres: Iterable[str] | None = None,
    language: Iterable[str] | None = None,
    exclude_languages: Iterable[str] | None = None,
    allow_unlabelled_language: bool = False,
    bpm: float | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    use_history: bool = True,
    seeds: list[dict[str, Any]] | None = None,
    resolved: dict[str, Any] | None = None,
    graph_conn: Any = None,
    exclude_index: dict[str, list[str | None]] | None = None,
) -> dict[str, Any]:
    """Produce a mood-shaped, ordered set of new songs.

    `seeds` overrides where the search starts. By default seeds are drawn from
    the whole library (pick_seeds); pass them in to seed from somewhere
    narrower, e.g. one playlist's own tracks (pick_seeds_from_playlist).

    `resolved` passes in an already-resolved target (from resolve_target). A
    caller that needed the mood *before* calling build -- to choose seeds with
    it, say -- must pass it back rather than let it be resolved twice: an
    inferred mood reads listening history, so resolving again is both a second
    API call and a chance for the two to disagree.
    """
    if resolved is None:
        resolved = resolve_target(conn, yt if use_history else None, feeling, vector, context)
    target = resolved["target"]

    if seeds is None:
        seeds = pick_seeds(conn, target, genres=genres)
    notes = list(resolved.get("evidence", []))

    # The graph, threaded through exactly as the v1 similarity tools do. v6
    # wired it into `recommend_from_song`/`recommend_from_playlist` and not
    # into this path, which was invisible on YouTube -- native signals fill the
    # pool -- and total on a backend whose `capabilities()` are `(none)`:
    # measured on Spotify, 0 candidates and therefore 0 songs, on every mood.
    per_seed = signals.gather_seeds(
        yt,
        [seed["videoId"] for seed in seeds],
        graph_conn=graph_conn,
        seed_meta={
            seed["videoId"]: {
                "title": seed.get("title"),
                # `artists` arrives from the store as one joined credit
                # ("AP Dhillon & Gurinder Gill"), and the graph wants a list.
                # Handing the string straight over makes `artists[0]` the
                # letter "A" -- the same string-is-iterable bug that produced
                # "A & P &  & D & h..." in store._artist_names and a language
                # filter for "e, n, g, l, i, s, h". Third time; hence match.py.
                "artists": match.artist_list(seed.get("artists")),
            }
            for seed in seeds if seed.get("videoId")
        },
    )

    merged = signals._merge_and_score(per_seed)

    for candidate in atlas_neighbours(conn, target):
        existing = merged.get(candidate["videoId"])
        if existing is None:
            merged[candidate["videoId"]] = {
                "videoId": candidate["videoId"], "title": candidate["title"],
                "artists": candidate["artists"], "album": candidate["album"],
                "sources": {ATLAS_SOURCE}, "score": 1,
            }
        else:
            existing["sources"].add(ATLAS_SOURCE)
            existing["score"] += 1

    # A seed must never be recommended back. The library exclusion normally
    # covers this (seeds come from the library), but atlas_neighbours can
    # resurface one, and a caller may pass a narrower exclusion set.
    blocked = set(exclude) | {seed["videoId"] for seed in seeds}
    # **Exclusion is two-stage here too, and this is stage one.** `blocked`
    # holds provider ids; a graph candidate is keyed `graph:<deezer id>` and
    # has no provider id yet, so testing it against `blocked` always passes and
    # the never-recommend-a-library-song guarantee would silently stop applying
    # to every graph-sourced candidate -- the exact hole signals._finalize
    # exists to close on the similarity path. Stage two is by id, after
    # resolution, below.
    pool = {}
    for vid, c in merged.items():
        if c.get("videoId"):
            if vid in blocked:
                continue
        elif exclude_index is not None and match.matches_any(
            c.get("title"), (c.get("artists") or [None])[0], exclude_index
        ):
            continue
        pool[vid] = c

    pool, variants_collapsed = signals._collapse_variants(pool)
    if variants_collapsed:
        notes.append(
            f"Collapsed {variants_collapsed} remix/feature variant(s) down to one per song."
        )

    if not pool:
        return {
            "target": {k: round(v, 3) for k, v in target.items()},
            "target_origin": resolved["origin"],
            "described": moodspace.describe(target),
            "arc": arc, "seeds": seeds,
            "notes": notes + ["No candidates survived the library exclusion."],
            "filters": {"language": {"applied": False}, "tempo": {"applied": False}},
            "match_quality": {"genuine": 0, "requested": limit, "fluff_cap": math.ceil(limit * FLUFF_CAP_RATIO), "fluff_used": 0},
            "songs": [],
        }

    moods = label.resolve_or_derive(conn, pool.keys())
    # A graph candidate has no provider id, so `label` has nothing to look up
    # for it -- but the graph atlas already holds a mood for that exact Deezer
    # track. Reading it here is the same id bridge
    # `graph_atlas.propagate_to_provider` walks offline for the library, done
    # inline for candidates, and it costs no network call. Without it every
    # graph candidate would arrive unrated and be capped as filler, which on a
    # backend where they are the *only* candidates means mood ranking is blind
    # exactly where it does all of the work.
    moods.update(_graph_moods(graph_conn, pool))
    known_artists = _library_artists(conn)
    affinity = artist_affinity(conn)

    candidates = []
    for video_id, candidate in pool.items():
        entry = moods.get(video_id)
        artist = label.primary_artist(" & ".join(candidate["artists"] or []))
        known = bool(artist and artist in known_artists)
        learned = affinity.get(artist, 1.0) if artist else 1.0
        boost = (KNOWN_ARTIST_BOOST if known else 1.0) * learned
        candidates.append(
            {
                # A graph candidate's pool key is `graph:<deezer id>`, not a
                # provider id. It must stay null until resolution, or the key
                # leaks into results and into the exclusion check as if it
                # were one.
                "videoId": candidate.get("videoId"),
                **({"graphRef": candidate["graphRef"]} if candidate.get("graphRef") else {}),
                "title": candidate["title"],
                "artists": candidate["artists"],
                "album": candidate.get("album"),
                "sources": sorted(candidate["sources"]),
                "signal_score": candidate["score"],
                "mood": entry["vector"] if entry else None,
                "mood_source": entry["source"] if entry else None,
                "base_score": candidate["score"] * boost,
                "known_artist": known,
                # Surfaced so a nudge from learned feedback is explainable
                # rather than an unexplained reordering. Omitted when 1.0.
                **({"affinity": round(learned, 3)} if learned != 1.0 else {}),
            }
        )

    # Keep a generous shortlist so the sequencer has room to satisfy both the
    # arc and the per-artist cap.
    candidates.sort(key=lambda c: -c["base_score"])
    shortlist = candidates[: max(limit * CANDIDATES_PER_SLOT, 200)]

    shortlist, language_report = filters.apply_language(
        conn, shortlist, want=language, exclude=exclude_languages,
        allow_unlabelled=allow_unlabelled_language,
    )
    if language_report["applied"]:
        notes.append(_language_note(language_report, limit))

    shortlist, tempo_report = filters.apply_tempo(
        conn, shortlist, target_bpm=bpm, bpm_min=bpm_min, bpm_max=bpm_max
    )
    if tempo_report["applied"]:
        notes.append(_tempo_note(tempo_report))
    shortlist.sort(key=lambda c: -c["base_score"])

    slot_targets = arc_module.targets(target, arc, limit)
    ordered, unresolved = _sequence_and_resolve(
        yt, shortlist, slot_targets, blocked, graph_conn is not None
    )
    if unresolved:
        notes.append(
            f"Dropped {unresolved} music-graph candidate(s) that couldn't be matched to a song "
            "on this service (or turned out to be in your library after matching)."
        )

    ordered, match_quality = _cap_fluff(ordered, requested=limit)
    if match_quality["genuine"] < limit:
        notes.append(
            f"Only {match_quality['genuine']} songs genuinely matched this mood "
            f"(rated, with a real fit above the {arc_module.UNRATED_FIT:.2f} unrated baseline); "
            f"capped filler at {match_quality['fluff_cap']} (25% of the {limit} requested), "
            f"so {len(ordered)} were returned total."
        )

    # Remember what we served. Without this, explain_recommendation knows a
    # song was recommended but not what it was.
    store.upsert_tracks(conn, ordered)

    rated = sum(1 for song in ordered if song["rated"])
    if ordered and rated < len(ordered):
        notes.append(
            f"{rated}/{len(ordered)} picks have a mood label; the rest were ranked on signal "
            "agreement alone. Run scripts/label_library.py to raise that."
        )

    return {
        "target": {k: round(v, 3) for k, v in target.items()},
        "target_origin": resolved["origin"],
        "described": moodspace.describe(target),
        "arc": arc,
        "seeds": [
            {"title": s["title"], "artists": s["artists"], "fit": round(s["fit"], 3),
             "seed_score": round(s["seed_score"], 3)}
            for s in seeds
        ],
        "notes": notes,
        "filters": {"language": language_report, "tempo": tempo_report},
        "match_quality": match_quality,
        "songs": ordered,
    }
