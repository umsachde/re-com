"""Fuzzy song identity: is this credit the same song as that credit?

Every part of re-com that crosses a boundary between two catalogues needs this,
and each one grew its own copy:

  - `signals.py` needs it *within* one provider, because YouTube carries the
    same track many times over (topic channels, remasters, "(Official Video)").
    Observed live: seeding on "Kryptonite" returned Kryptonite, under a
    different videoId, so excluding the seed by id alone was not enough.
  - `tempo.py` needs it between a provider and Deezer, because the artist gate
    on Deezer search is too strict for real YouTube credits -- compilation
    uploads ("Billboard Top 100 Hits") and odd separators ("Shankar Mahadevan |
    Alyssa Men") match nothing, which reported 318 library songs as unmatched
    when most were simply findable by title.
  - v6's music graph needs it in *both* directions at once: Deezer supplies
    candidates as title+artist text, and they have to be checked against the
    library and then resolved back to provider ids.

PLAN.md warned that the graph bridge would become a third copy of this logic.
It is one module instead, and `signals.py`/`tempo.py` delegate here rather than
keeping their own. That also removes a genuine inversion: `tempo._same_title`
used to `import signals` *inside the function body* purely to dodge a circular
import, a low-level module reaching up into a higher-level one.

**The matching is deliberately loose, and it will be wrong sometimes.** Every
caller treats a false positive as "drop a candidate" rather than "return the
wrong song", so erring toward matching is the safe direction -- see
`matches_any`, where an unknown artist on either side counts as a match.
"""

from typing import Any, Iterable

# Bracketed qualifiers to strip: "(Official Video)", "[Remastered]", ...
_BRACKETS = (("(", ")"), ("[", "]"))

# Credit separators after which everything is a feature/version qualifier
# rather than part of the song's identity.
_QUALIFIER_MARKERS = (" - ", " feat.", " ft.", " with ")


# How `store` joins several credits into the one `track.artists` column.
CREDIT_SEPARATOR = " & "


def artist_list(artists: Any) -> list[str]:
    """Artist names as a list, from either a list or one joined credit.

    The store keeps `artists` as a single joined string ("AP Dhillon & Gurinder
    Gill") while every graph and provider path wants a list. Strings are
    iterable, so handing the joined form to code expecting a list silently
    yields *letters* -- this project has now hit that three separate times
    (`store._artist_names` splitting "AP Dhillon" into "A & P & ...",
    `taxonomy.as_languages` filtering for "e, n, g, l, i, s, h", and a seed
    artist arriving as "A"). Normalising at one funnel is the fix that
    generalises, which is why it lives here with the rest of song identity.
    """
    if not artists:
        return []
    if isinstance(artists, str):
        return [part.strip() for part in artists.split(CREDIT_SEPARATOR) if part.strip()]
    return [str(a).strip() for a in artists if a and str(a).strip()]


def song_key(title: str) -> str:
    """Normalise a title to its identity: drop bracketed qualifiers,
    feature credits, punctuation and case.

    "Dead and Gone", "Dead and Gone (feat. Justin Timberlake)" and
    "Dead and Gone [Remastered]" all collapse to `deadandgone`.
    """
    lowered = title.lower()
    for opener, closer in _BRACKETS:
        while opener in lowered and closer in lowered[lowered.index(opener):]:
            start = lowered.index(opener)
            end = lowered.index(closer, start)
            lowered = (lowered[:start] + " " + lowered[end + 1:]).strip()
    for marker in _QUALIFIER_MARKERS:
        if marker in lowered:
            lowered = lowered.split(marker)[0]
    return "".join(ch for ch in lowered if ch.isalnum())


def same_title(candidate: str | None, wanted: str | None) -> bool:
    """Normalised title equality. Both sides must be present.

    This is the guard on `tempo.py`'s title-only Deezer fallback: without it,
    searching by title alone could quietly attach some other song's tempo.
    """
    return bool(candidate and wanted and song_key(candidate) == song_key(wanted))


def artist_matches(candidate: str | None, wanted: str | None) -> bool:
    """Loose credit match -- case-insensitive substring, either direction.

    Catalogues format the same artist differently ("3 Doors Down" vs
    "3 Doors Down Topic", "&" vs "feat."), so substring containment is the
    only thing that survives across sources.

    An empty `wanted` matches anything: callers that don't know the artist
    they're looking for must not be handed zero results.

    An empty `candidate`, however, matches nothing -- and that is a deliberate
    fix, not a faithful port. `tempo._artist_matches` did a bare substring test,
    and `"" in anything` is always True, so a Deezer hit carrying no artist name
    counted as matching whatever artist was asked for. On the tempo path that
    meant a nameless hit could pass the artist gate and attach some other
    song's BPM. Asymmetric on purpose: not knowing what you want is a reason to
    accept anything, but a source not saying who performed a track is not
    evidence that it is the right track.
    """
    if not wanted:
        return True
    if not candidate:
        return False
    a, b = candidate.lower(), wanted.lower()
    return a in b or b in a


def artist_names_match(candidate_artists: Iterable[str | None], target_names_lower: Iterable[str]) -> bool:
    """Whether any of a candidate's artist credits matches any target name.

    `target_names_lower` must already be lowercased -- callers filtering a
    large candidate pool lower the (small, fixed) target list once rather than
    per candidate.
    """
    targets = list(target_names_lower)
    for name in candidate_artists:
        if not name:
            continue
        name_lower = name.lower()
        if any(t in name_lower or name_lower in t for t in targets):
            return True
    return False


def same_song(
    title_a: str | None, artist_a: str | None, title_b: str | None, artist_b: str | None
) -> bool:
    """Whether two credits are the same song under different uploads.

    Titles must match on their normalised key. Artists only have to match
    loosely, and a missing artist on either side is accepted -- catalogues
    routinely omit the credit entirely, and refusing to match on that basis
    would let known duplicates through.
    """
    if not title_a or not title_b:
        return False
    if song_key(title_a) != song_key(title_b):
        return False
    if not artist_a or not artist_b:
        return True
    return artist_matches(artist_a, artist_b)


# --- song-key indexes -------------------------------------------------------
#
# v6 needs to ask "is this song already in the library?" about a candidate that
# has no provider id yet -- graph candidates arrive from Deezer as title+artist
# text, and resolving every one of them to a provider id just to check would be
# exactly the eager resolution PLAN.md rules out (a 500-candidate pool, one
# search each). So the check happens on text first, against an index built once
# per request from data the store already holds.


def build_index(rows: Iterable[tuple[str | None, Any]]) -> dict[str, list[str | None]]:
    """Index (title, artists) pairs by song key for `matches_any`.

    `artists` may be a string or a list of names -- the store keeps them
    joined, provider payloads keep them split, and both are accepted so
    callers don't have to normalise first.
    """
    index: dict[str, list[str | None]] = {}
    for title, artists in rows:
        if not title:
            continue
        key = song_key(title)
        if not key:
            continue
        if isinstance(artists, (list, tuple, set)):
            credits: list[str | None] = [a for a in artists if a] or [None]
        else:
            credits = [artists or None]
        index.setdefault(key, []).extend(credits)
    return index


def matches_any(title: str | None, artist: str | None, index: dict[str, list[str | None]]) -> bool:
    """Whether (title, artist) appears in a `build_index` index.

    Same semantics as `same_song`, applied against many candidates at once:
    the title key must match exactly, the artist only loosely, and an unknown
    artist on either side counts as a match.

    Erring toward True is deliberate. A false positive drops one candidate
    from a pool of hundreds; a false negative breaks the guarantee this whole
    project exists for -- never recommending something already in the library.
    """
    if not title:
        return False
    key = song_key(title)
    if not key:
        return False
    credits = index.get(key)
    if credits is None:
        return False
    if not artist:
        return True
    return any(not other or artist_matches(other, artist) for other in credits)
