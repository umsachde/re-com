"""Mood labelling with Claude.

The atlas (see atlas.py) is broad but shallow: a measured 60-playlist sample
covered only 4.1% of this user's library, and songs that were covered usually
carried a single mood tag -- enough to snap to an anchor, not enough to place a
song in the space. The gap is also not random. It falls on the Punjabi,
Bollywood and Reggae catalogue that YouTube's English-centric mood playlists
barely touch, which is exactly the part of the library that matters most here.

Language understanding closes that gap. Given a title, artist, whatever atlas
evidence exists and a lyric excerpt, Claude can place a song on all four axes
in any language, and can tell defiance from despair where a tag cannot.

This layer is optional. Without the `anthropic` package or credentials the rest
of v2 still runs on atlas evidence alone -- degraded, and it says so, rather
than failing.
"""

import json
import os
from typing import Any

import moodspace

# Default to the strongest model: which model labels your library is a decision
# about quality, and it belongs to the user, not to this file. Override with
# RECOM_JUDGE_MODEL to trade accuracy for cost.
MODEL = os.environ.get("RECOM_JUDGE_MODEL", "claude-opus-5")
EFFORT = os.environ.get("RECOM_JUDGE_EFFORT", "low")

# Lyrics make these prompts long, so batches stay modest.
BATCH_SIZE = int(os.environ.get("RECOM_JUDGE_BATCH", 12))

SYSTEM = """You place songs in a four-axis mood space. You are given each song's \
title, artists, any mood-playlist tags YouTube Music has applied to it, and \
where available an excerpt of its lyrics.

Axes:
- valence  (-1..1): -1 despairing, 0 neutral, 1 euphoric.
- energy    (0..1): 0 completely still, 1 frantic.
- tension   (0..1): 0 resolved and warm, 1 anxious or aggressive. This is what \
separates an angry song from an excited one; they share high energy.
- depth     (0..1): 0 background wallpaper, 1 lyric-forward and demanding \
attention.

Also return confidence (0..1): how sure you are given the evidence. Lyrics in a \
language you read well and a recognisable artist justify high confidence; a bare \
title with no lyrics and no tags justifies low confidence. Be honest -- a \
confident wrong label is worse than an uncertain right one.

Judge the song as a listener experiences it. Lyrics carry irony and defiance \
that tags miss: a defiant breakup song is not the same as a despairing one, and \
an upbeat production with devastated lyrics is not simply happy. Songs in any \
language are in scope; judge them on their own terms, not by how they would read \
translated."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "songs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "valence": {"type": "number"},
                    "energy": {"type": "number"},
                    "tension": {"type": "number"},
                    "depth": {"type": "number"},
                    "confidence": {"type": "number"},
                },
                "required": ["video_id", "valence", "energy", "tension", "depth", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["songs"],
    "additionalProperties": False,
}


class JudgeUnavailable(RuntimeError):
    """Raised when labelling is requested but Claude isn't reachable."""


def _client():
    try:
        import anthropic
    except ImportError as e:
        raise JudgeUnavailable(
            "The 'anthropic' package isn't installed. Run: pip install -e '.[llm]'"
        ) from e
    try:
        return anthropic.Anthropic()
    except Exception as e:  # noqa: BLE001 - surfaced as a single actionable message
        raise JudgeUnavailable(
            "No Anthropic credentials found. Run 'ant auth login' or set ANTHROPIC_API_KEY."
        ) from e


def available() -> bool:
    """Whether mood labelling can run at all, for callers that degrade instead of fail."""
    try:
        _client()
        return True
    except JudgeUnavailable:
        return False


def _render(track: dict[str, Any]) -> str:
    lines = [f"video_id: {track['video_id']}", f"title: {track.get('title') or 'unknown'}"]
    if track.get("artists"):
        lines.append(f"artists: {track['artists']}")
    if track.get("moods"):
        lines.append(f"youtube mood tags: {', '.join(track['moods'])}")
    if track.get("playlists"):
        lines.append(f"appears on playlists named: {'; '.join(track['playlists'][:6])}")
    if track.get("lyrics"):
        lines.append(f"lyrics excerpt:\n{track['lyrics']}")
    else:
        lines.append("lyrics: unavailable")
    return "\n".join(lines)


def label_batch(tracks: list[dict[str, Any]], client: Any = None) -> dict[str, tuple[dict[str, float], float]]:
    """Label a batch of songs. Returns video_id -> (vector, confidence).

    Songs the model omits or returns malformed are simply absent from the
    result -- a caller that gets nothing back for a track leaves it unlabelled
    rather than recording a fabricated vector.
    """
    if not tracks:
        return {}

    client = client or _client()
    body = "\n\n---\n\n".join(_render(t) for t in tracks)

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": f"Place each of these songs.\n\n{body}"}],
    )

    if response.stop_reason == "refusal":
        return {}

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return {}

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}

    labelled: dict[str, tuple[dict[str, float], float]] = {}
    for entry in payload.get("songs", []):
        video_id = entry.get("video_id")
        if not video_id:
            continue
        try:
            vector = moodspace.vector(
                valence=float(entry["valence"]),
                energy=float(entry["energy"]),
                tension=float(entry["tension"]),
                depth=float(entry["depth"]),
            )
            confidence = max(0.0, min(1.0, float(entry.get("confidence", 0.5))))
        except (KeyError, TypeError, ValueError):
            continue
        labelled[video_id] = (vector, confidence)
    return labelled


def batches(items: list[Any], size: int = BATCH_SIZE):
    for start in range(0, len(items), size):
        yield items[start : start + size]
