"""The mood coordinate system.

A flat tag list ("sad", "hype", "chill") can't express distance, blending or
trajectory, so mood is modelled as a small continuous vector instead. That one
choice is what makes the rest of v2 arithmetic rather than special cases:
"how close is this song to how you feel" is a distance, "meet them here and
move them there" is an interpolation.

Axes, and why each earns its slot:

  valence  -1..1  despairing -> euphoric.       The primary emotional sign.
  energy    0..1  still -> frantic.             Arousal; with valence this is
                                                Russell's circumplex.
  tension   0..1  resolved -> anxious.          Separates angry from excited.
                                                Two axes cannot tell aggressive
                                                workout rap from joyful party
                                                pop; they sit in the same place.
  depth     0..1  wallpaper -> lyric-forward.   Decides whether the listener
                                                gets something to think along
                                                with or something to work behind.
"""

from typing import Any, Iterable

AXES = ("valence", "energy", "tension", "depth")

# Each axis's full span, used to normalise differences so that valence (which
# runs -1..1) doesn't count double against axes that run 0..1.
_SPAN = {"valence": 2.0, "energy": 1.0, "tension": 1.0, "depth": 1.0}

# How much each axis counts toward "does this song match that mood". Valence
# and energy dominate perception; tension and depth are corrections, important
# for avoiding specific wrong answers rather than for finding right ones.
_WEIGHT = {"valence": 1.0, "energy": 1.0, "tension": 0.7, "depth": 0.6}

# How much being *distinctly* the requested mood counts, relative to simply
# being close to it. Above ~1 the distinctiveness term dominates, which is
# intended: raw fit alone demonstrably picks the same bland songs for every mood.
DISTINCTIVENESS_WEIGHT = 1.5

# How much of the seed score confidence can swing. At 0.3, a maximally
# confident label is worth 1.0x and a wholly unconfident one 0.7x.
CONFIDENCE_FLOOR = 0.3

_BOUNDS = {"valence": (-1.0, 1.0), "energy": (0.0, 1.0), "tension": (0.0, 1.0), "depth": (0.0, 1.0)}

# Hand-authored positions for YouTube Music's own "Moods & moments" taxonomy.
# These are the bridge between a free corpus of mood-labelled playlists and the
# vector space -- a first draft, meant to be tuned against real listening.
# Seasonal categories (Christmas, Halloween) are deliberately absent: they
# describe an occasion, not an emotional state, and have no honest position here.
ANCHORS: dict[str, dict[str, float]] = {
    "Sad":       {"valence": -0.70, "energy": 0.25, "tension": 0.35, "depth": 0.85},
    "Chill":     {"valence":  0.25, "energy": 0.25, "tension": 0.15, "depth": 0.35},
    "Sleep":     {"valence":  0.10, "energy": 0.05, "tension": 0.05, "depth": 0.15},
    "Focus":     {"valence":  0.05, "energy": 0.35, "tension": 0.20, "depth": 0.10},
    "Commute":   {"valence":  0.30, "energy": 0.55, "tension": 0.30, "depth": 0.40},
    "Feel good": {"valence":  0.75, "energy": 0.60, "tension": 0.10, "depth": 0.35},
    "Romance":   {"valence":  0.55, "energy": 0.35, "tension": 0.20, "depth": 0.70},
    "Energize":  {"valence":  0.60, "energy": 0.90, "tension": 0.45, "depth": 0.30},
    "Workout":   {"valence":  0.35, "energy": 0.95, "tension": 0.70, "depth": 0.20},
    "Party":     {"valence":  0.70, "energy": 0.85, "tension": 0.30, "depth": 0.15},
    "Gaming":    {"valence":  0.10, "energy": 0.75, "tension": 0.65, "depth": 0.15},
}

# Moods worth crawling: exactly those we can place in the space.
CRAWLABLE_MOODS = tuple(ANCHORS)


def clamp(vector: dict[str, float]) -> dict[str, float]:
    return {a: max(_BOUNDS[a][0], min(_BOUNDS[a][1], float(vector[a]))) for a in AXES}


def vector(valence: float = 0.0, energy: float = 0.5, tension: float = 0.3, depth: float = 0.4) -> dict[str, float]:
    return clamp({"valence": valence, "energy": energy, "tension": tension, "depth": depth})


def is_vector(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(a), (int, float)) and not isinstance(value.get(a), bool) for a in AXES
    )


def distance(a: dict[str, float], b: dict[str, float]) -> float:
    """Weighted, span-normalised Euclidean distance. 0 is identical; the
    theoretical maximum is sqrt(sum of weights) ~= 1.82."""
    return sum(_WEIGHT[ax] * ((a[ax] - b[ax]) / _SPAN[ax]) ** 2 for ax in AXES) ** 0.5


_MAX_DISTANCE = sum(_WEIGHT.values()) ** 0.5


def fit(a: dict[str, float], b: dict[str, float]) -> float:
    """Distance expressed as a 0..1 score, 1 being a perfect match. This is the
    form ranking wants -- a multiplier, not a penalty."""
    return max(0.0, 1.0 - distance(a, b) / _MAX_DISTANCE)


def relative_fit(vec: dict[str, float], target: dict[str, float]) -> float:
    """Fit to `target`, minus how well this vector fits moods in general.

    Raw fit has a serious failure mode as a *selection* score: a vector near the
    centre of the space fits everything moderately well, so bland songs beat
    distinctive ones for every mood. Measured on real data -- "Africa" by Toto
    sits at valence 0.53 / energy 0.53, fits all eleven anchors between 0.57 and
    0.92, and was the top seed for heartbroken, angry, Party AND Focus alike.
    Appearing in many mood playlists also maxes out its confidence, compounding
    the problem.

    Subtracting the vector's mean fit across all anchors removes that advantage.
    A central vector scores near zero; a vector that is distinctly one mood
    scores high for that mood and negative for the others.
    """
    baseline = sum(fit(vec, anchor) for anchor in ANCHORS.values()) / len(ANCHORS)
    return fit(vec, target) - baseline


def seed_score(vec: dict[str, float], target: dict[str, float], confidence: float = 1.0) -> float:
    """How good a song is as a *seed* for a mood, as opposed to how close it is.

    Absolute fit still matters -- a distinctive song for the wrong mood is no
    use -- but distinctiveness is weighted more heavily, because that is what
    raw fit systematically undervalues. Confidence is softened rather than
    multiplied straight in: a confident bland label should not beat an
    uncertain distinctive one.
    """
    combined = fit(vec, target) + DISTINCTIVENESS_WEIGHT * relative_fit(vec, target)
    return combined * (1.0 - CONFIDENCE_FLOOR + CONFIDENCE_FLOOR * max(0.0, min(1.0, confidence)))


def blend(weighted: Iterable[tuple[dict[str, float], float]]) -> dict[str, float] | None:
    """Weighted mean of several vectors. Returns None if nothing carried weight."""
    total = 0.0
    acc = dict.fromkeys(AXES, 0.0)
    for vec, weight in weighted:
        if weight <= 0:
            continue
        total += weight
        for ax in AXES:
            acc[ax] += vec[ax] * weight
    if total <= 0:
        return None
    return clamp({ax: acc[ax] / total for ax in AXES})


def lerp(a: dict[str, float], b: dict[str, float], t: float) -> dict[str, float]:
    """Point t of the way from a to b. This is what draws a mood arc."""
    t = max(0.0, min(1.0, t))
    return clamp({ax: a[ax] + (b[ax] - a[ax]) * t for ax in AXES})


def from_atlas_counts(counts: dict[str, int]) -> tuple[dict[str, float], float] | None:
    """Turn "this song appeared in 6 Chill playlists and 1 Focus playlist" into
    a vector plus a confidence.

    Playlist counts are the weights, so a song that keeps showing up under one
    mood lands squarely on that anchor while a song scattered across several
    lands between them. Confidence saturates: one playlist is a rumour, six are
    a fact, and sixty aren't ten times more certain than six.
    """
    weighted = [(ANCHORS[m], float(n)) for m, n in counts.items() if m in ANCHORS and n > 0]
    if not weighted:
        return None
    vec = blend(weighted)
    if vec is None:
        return None
    total = sum(n for _, n in weighted)
    confidence = min(1.0, total / 6.0)
    return vec, confidence


def nearest_anchors(vec: dict[str, float], n: int = 2) -> list[tuple[str, float]]:
    """The n closest named moods, for explaining a vector in words."""
    scored = sorted(((name, fit(vec, anchor)) for name, anchor in ANCHORS.items()), key=lambda p: -p[1])
    return scored[:n]


def describe(vec: dict[str, float]) -> str:
    """A short human phrase for a vector -- used so recommendations can say why
    they were chosen instead of returning bare numbers."""
    names = [name for name, _ in nearest_anchors(vec, 2)]
    energy = "low-energy" if vec["energy"] < 0.35 else "high-energy" if vec["energy"] > 0.7 else "mid-energy"
    tone = "downbeat" if vec["valence"] < -0.2 else "upbeat" if vec["valence"] > 0.3 else "even"
    edge = ", edgy" if vec["tension"] > 0.6 else ""
    return f"{tone}, {energy}{edge} (closest to {names[0]}/{names[1]})"
