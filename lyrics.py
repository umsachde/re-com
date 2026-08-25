"""Lyric fetching and caching.

Lyrics are the deepest mood signal available for a song: no audio features
exist anywhere in the YouTube Music API, but the words are right there, and
they say what a song is about in a way tempo never could.

They cost two API calls each (watch playlist -> lyrics browse id -> lyrics), so
they are fetched once and cached permanently, including the negative result --
a song with no lyrics should be discovered to have none exactly once.
"""

from typing import Any

import requests
from ytmusicapi.exceptions import YTMusicError

import store
from provider import ProviderError

# `ProviderError` matters as of v6: scripts/label_library.py now passes a
# Provider (server._client()) rather than a raw ytmusicapi client, and a
# provider translates its own failures into ProviderError. Without it here, a
# transient provider failure would crash the labelling pass instead of leaving
# one song unlabelled -- which is the whole point of catching these.
#
# YTMusicError stays because this module is also called directly by the
# YouTube-native offline scripts, which still hold a real ytmusicapi client.
_TRANSIENT = (ProviderError, YTMusicError, requests.exceptions.RequestException)

# Enough to establish mood without paying for a full lyric sheet on every song.
EXCERPT_CHARS = 900


def fetch(yt: Any, video_id: str) -> tuple[str | None, str | None]:
    """Fetch lyrics for a song. Returns (text, source); (None, None) if absent.

    A backend with no lyric support at all (Spotify exposes none) is treated
    exactly like a song with no lyrics: this layer simply doesn't contribute,
    and `label.py` falls through to the next mood source. Absence of a
    capability must degrade, not raise.
    """
    getter = getattr(yt, "get_lyrics", None)
    if getter is None:
        return None, None

    try:
        watch = yt.get_watch_playlist(videoId=video_id, limit=1)
    except _TRANSIENT:
        return None, None

    browse_id = (watch or {}).get("lyrics")
    if not browse_id:
        return None, None

    try:
        result = getter(browse_id)
    except _TRANSIENT:
        return None, None

    text = (result or {}).get("lyrics")
    return (text, (result or {}).get("source")) if text else (None, None)


def get_or_fetch(conn: Any, yt: Any, video_id: str) -> str | None:
    """Cached lyrics, fetching only on a genuine first look.

    A cached row with `available = 0` is a definitive "this track has no
    lyrics" and short-circuits -- otherwise every unlabelled instrumental would
    cost two API calls on every pass.
    """
    cached = store.get_lyrics(conn, video_id)
    if cached is not None:
        return cached["text"] if cached["available"] else None

    text, source = fetch(yt, video_id)
    store.put_lyrics(conn, video_id, text, source)
    return text


def excerpt(text: str | None, limit: int = EXCERPT_CHARS) -> str | None:
    """Trim lyrics to a mood-bearing excerpt, cut on a line boundary."""
    if not text:
        return None
    cleaned = "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rsplit("\n", 1)[0]
