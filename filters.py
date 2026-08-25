"""Language and tempo filters applied to candidate songs.

Both answer requests the mood engine alone can't: "songs like this Punjabi
track, but only English ones", and "keep it around 120bpm".

The two behave differently on missing data, deliberately:

  language  Strict by default. Someone asking for English only wants a
            guarantee, and unlabelled candidates seeded from a Punjabi song
            are overwhelmingly Punjabi -- keeping them would hand back exactly
            what was excluded. The count of songs dropped for having no label
            is reported rather than hidden, and `allow_unlabelled` relaxes it.

  tempo     Lenient by default. Deezer has no BPM for most of the non-English
            catalogue (measured: 6/6 on Pop, 1/6 on Punjabi), so dropping
            unknown-tempo songs would quietly delete whole languages from the
            results. Unknown tempo keeps a song and simply doesn't score it.

Tempo is also resolved for a shortlist only. Each lookup costs two Deezer
requests, so pricing a 300-song candidate pool would take minutes.
"""

from typing import Any, Iterable

import label
import store
import taxonomy
import tempo as tempo_mod

# How much tempo agreement can swing a candidate's score when no hard range is
# given. Kept modest: tempo is a similarity signal, not the point of the search.
TEMPO_WEIGHT = 0.35

# Candidates to price tempo for. Beyond this the lookups dominate latency.
TEMPO_SHORTLIST = 40


def language_of(conn: Any, candidate: dict[str, Any], artist_languages: dict[str, str]) -> dict[str, Any] | None:
    """Resolve a candidate's language, falling back to its artist's."""
    video_id = candidate.get("videoId")
    title = candidate.get("title")
    artists = " & ".join(candidate.get("artists") or [])

    found = taxonomy.resolve_language(conn, video_id, title, artists)
    if found:
        return found

    artist = label.primary_artist(artists)
    if artist and artist in artist_languages:
        return {"language": artist_languages[artist], "source": taxonomy.SOURCE_ARTIST}
    return None


def apply_language(
    conn: Any,
    candidates: list[dict[str, Any]],
    want: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    allow_unlabelled: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep only candidates in the requested language(s)."""
    # A bare string here would be iterated character by character -- see
    # taxonomy.as_languages. Normalised at this single funnel rather than at
    # each of the three tools that reach it.
    want = taxonomy.as_languages(want)
    exclude = taxonomy.as_languages(exclude)
    if not want and not exclude:
        return candidates, {"applied": False}

    artist_languages = taxonomy.artist_languages(conn)
    kept, dropped, unknown = [], 0, 0

    for candidate in candidates:
        found = language_of(conn, candidate, artist_languages)
        verdict = taxonomy.matches(found["language"] if found else None, want, exclude)
        if verdict is None:
            unknown += 1
            if allow_unlabelled:
                kept.append({**candidate, "language": None, "language_source": None})
            continue
        if not verdict:
            dropped += 1
            continue
        kept.append({**candidate, "language": found["language"], "language_source": found["source"]})

    return kept, {
        "applied": True,
        "want": list(want) if want else None,
        "exclude": list(exclude) if exclude else None,
        "kept": len(kept),
        "dropped_wrong_language": dropped,
        "dropped_unlabelled": 0 if allow_unlabelled else unknown,
        "allow_unlabelled": allow_unlabelled,
    }


def apply_tempo(
    conn: Any,
    candidates: list[dict[str, Any]],
    target_bpm: float | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    resolve_missing: bool = True,
    shortlist: int = TEMPO_SHORTLIST,
    sleep=None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Score and optionally bound candidates by tempo.

    Songs with no known tempo keep their place and are simply not scored on it
    -- see the module docstring for why that asymmetry with language is
    intentional rather than an oversight.
    """
    if target_bpm is None and bpm_min is None and bpm_max is None:
        return candidates, {"applied": False}

    known = store.get_tempos(conn, [c["videoId"] for c in candidates])

    if resolve_missing:
        pending = [c for c in candidates[:shortlist] if c["videoId"] not in known]
        for candidate in pending:
            kwargs = {"sleep": sleep} if sleep else {}
            bpm = tempo_mod.get_or_fetch(
                conn, candidate["videoId"], candidate.get("title") or "",
                " & ".join(candidate.get("artists") or []) or None, **kwargs,
            )
            if bpm:
                known[candidate["videoId"]] = bpm

    kept, out_of_range, unknown = [], 0, 0
    for candidate in candidates:
        bpm = known.get(candidate["videoId"])
        if bpm is None:
            unknown += 1
            kept.append({**candidate, "bpm": None, "tempo_fit": None})
            continue

        if tempo_mod.in_range(bpm, bpm_min, bpm_max) is False:
            out_of_range += 1
            continue

        fit = tempo_mod.similarity(bpm, target_bpm) if target_bpm else None
        scored = dict(candidate)
        scored["bpm"] = round(bpm, 1)
        scored["tempo_fit"] = round(fit, 3) if fit is not None else None
        if fit is not None:
            scored["base_score"] = candidate.get("base_score", 1.0) * (1 + TEMPO_WEIGHT * (fit - 0.5))
        kept.append(scored)

    return kept, {
        "applied": True,
        "target_bpm": target_bpm,
        "range": [bpm_min, bpm_max] if (bpm_min or bpm_max) else None,
        "with_known_tempo": len(candidates) - unknown,
        "unknown_tempo_kept": unknown,
        "dropped_out_of_range": out_of_range,
    }


def seed_tempo(conn: Any, video_id: str, title: str | None, artists: str | None, sleep=None) -> float | None:
    """Tempo of a seed song, so results can be matched to it."""
    kwargs = {"sleep": sleep} if sleep else {}
    return tempo_mod.get_or_fetch(conn, video_id, title or "", artists, **kwargs)
