"""Shaping a set of songs into a sequence that moves.

A mood-matched *set* is the obvious thing to build and the wrong one. The
iso-principle from music therapy: to shift someone's affect you meet them where
they are and move gradually. Opening with upbeat songs when someone is low gets
skipped -- it reads as being told to cheer up.

So a recommendation is an ordered list with a target mood per slot, and songs
are assigned to slots rather than merely filtered.
"""

from typing import Any, Callable

import moodspace

ARCS = ("mirror", "lift", "settle", "deepen", "hold")

# Where each arc ends up relative to where it started.
_DESTINATIONS: dict[str, Callable[[dict[str, float]], dict[str, float]]] = {
    "mirror": lambda start: dict(start),
    "lift": lambda start: moodspace.vector(
        valence=start["valence"] + 0.75, energy=start["energy"] + 0.35,
        tension=start["tension"] - 0.15, depth=start["depth"] - 0.2,
    ),
    "settle": lambda start: moodspace.vector(
        valence=start["valence"] + 0.15, energy=start["energy"] - 0.45,
        tension=start["tension"] - 0.3, depth=start["depth"] + 0.1,
    ),
    "deepen": lambda start: moodspace.vector(
        valence=start["valence"] - 0.3, energy=start["energy"] - 0.15,
        tension=start["tension"], depth=start["depth"] + 0.3,
    ),
    "hold": lambda start: dict(start),
}

# How far into the set the shift is complete. Arriving at the destination too
# early wastes the back half; too late and nothing actually moved.
_ARRIVAL = 0.85

# How far below the target a "hold" arc starts and ends. The target IS the peak
# -- a workout is warmup, peak, cooldown -- so the curve dips at the edges
# rather than pushing above the middle. Adding energy instead would clamp to
# 1.0 for exactly the high-energy moods this arc is for, flattening the curve.
_HOLD_DIP = 0.22

# The fit score an unlabelled candidate is assumed to have -- a guess, not
# evidence. Callers use this as the bar a *rated* song's real fit must clear
# to count as a genuine match rather than filler (see recommend.build).
UNRATED_FIT = 0.45


def targets(start: dict[str, float], arc: str, count: int) -> list[dict[str, float]]:
    """The mood each slot in the sequence is aiming at."""
    if arc not in ARCS:
        raise ValueError(f"Unknown arc {arc!r}. Choose from: {', '.join(ARCS)}.")
    if count <= 0:
        return []

    if arc == "hold":
        return [_hold_point(start, i / max(count - 1, 1)) for i in range(count)]

    end = _DESTINATIONS[arc](start)
    if count == 1:
        return [dict(start)]

    out = []
    for i in range(count):
        progress = i / (count - 1)
        out.append(moodspace.lerp(start, end, min(1.0, progress / _ARRIVAL)))
    return out


def _hold_point(start: dict[str, float], progress: float) -> dict[str, float]:
    # A single smooth hump: the middle sits at the target, the ends below it.
    hump = 1.0 - abs(progress - 0.5) * 2.0
    return moodspace.vector(
        valence=start["valence"], energy=start["energy"] - _HOLD_DIP * (1.0 - hump),
        tension=start["tension"], depth=start["depth"],
    )


def sequence(
    candidates: list[dict[str, Any]],
    slot_targets: list[dict[str, float]],
    max_per_artist: int = 2,
    unrated_fit: float = UNRATED_FIT,
) -> list[dict[str, Any]]:
    """Assign candidates to slots, best fit first, with variety constraints.

    Greedy per slot rather than a global optimum: the early slots matter most
    (they're where someone decides to keep listening), so they should get first
    pick rather than being traded away for a better total.

    Candidates with no mood are still eligible -- most of a discovery pool is
    unlabelled, and excluding them outright would mean recommending only the
    well-documented corner of the catalogue. They score as a middling fit and
    are marked so the caller can say so.
    """
    pool = list(candidates)
    used_artists: dict[str, int] = {}
    chosen: list[dict[str, Any]] = []

    for slot, target in enumerate(slot_targets):
        best_index, best_score = None, -1.0
        for index, candidate in enumerate(pool):
            artist = (candidate.get("artists") or [None])[0]
            key = (artist or "").lower()
            if key and used_artists.get(key, 0) >= max_per_artist:
                continue

            vector = candidate.get("mood")
            fit = moodspace.fit(vector, target) if vector else unrated_fit
            score = fit * candidate.get("base_score", 1.0)
            if score > best_score:
                best_index, best_score = index, score

        if best_index is None:
            break

        candidate = pool.pop(best_index)
        artist_key = ((candidate.get("artists") or [None])[0] or "").lower()
        if artist_key:
            used_artists[artist_key] = used_artists.get(artist_key, 0) + 1

        vector = candidate.get("mood")
        chosen.append(
            {
                **candidate,
                "slot": slot,
                "slot_target": {k: round(v, 3) for k, v in target.items()},
                "mood_fit": round(moodspace.fit(vector, target), 3) if vector else None,
                "rated": vector is not None,
            }
        )

    return chosen
