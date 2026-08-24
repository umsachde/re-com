"""The backend-agnostic seam between re-com's ranking/mood logic and whichever
streaming service it's actually talking to.

`signals.py`'s candidate generation (`_gather_seed_candidates`) and most of
`server.py` were written against `ytmusicapi.YTMusic`'s exact method
signatures and return shapes, because v1 only had one backend. `Provider`
formalizes that shape as an explicit interface -- not a new one, since the
proven candidate-generation logic (and its whole test suite) shouldn't be
rewritten just to add a second backend.

A new provider (Spotify, ...) implements this same interface by translating
its own API into these shapes -- see `spotify_client.py` for the concrete
example: `get_watch_playlist`/`get_song_related`/`get_artist` don't exist on
Spotify's Web API, so `SpotifyClient` builds equivalent responses out of
Spotify's recommendations/related-artists/artist-top-tracks endpoints. This
is the "shape spotify-mcp's tools to fit `_gather_seed_candidates`'s
expectations" option from PLAN.md's v3 notes, chosen over growing a new
backend-agnostic candidate-generation contract because it keeps every
existing signal/ranking/mood code path -- and every test covering it --
completely unchanged.
"""

import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

DEFAULT_PROVIDER = "youtube"


def active() -> str:
    """Which backend this process talks to, from RECOM_PROVIDER.

    Lives here rather than in server.py because `store.py` needs it too and
    must not import the server (which imports the store).
    """
    return os.environ.get("RECOM_PROVIDER", DEFAULT_PROVIDER).lower()


def scoped_path(base: Path, provider: str | None = None) -> Path:
    """Give a persistent file its own name per backend.

    Every id re-com stores -- library rows, cached exclusion sets, mood
    labels -- belongs to exactly one backend's namespace, and they are not
    interchangeable: a YouTube videoId is 11 chars, a Spotify track id is 22.
    Sharing one file between two provider instances is silently wrong rather
    than merely untidy. Measured before this existed: the Spotify instance
    read a 1,499-entry exclusion set of YouTube videoIds, none of which can
    ever match a Spotify id -- so the "never recommend something already in
    your library" guarantee, the whole point of this project, excluded
    nothing at all.

    The default provider keeps the original unsuffixed filename so existing
    installs keep their crawled atlas, labels and history rather than waking
    up to an empty store.
    """
    provider = provider or active()
    if provider == DEFAULT_PROVIDER:
        return base
    return base.with_name(f"{base.stem}-{provider}{base.suffix}")


class ProviderError(RuntimeError):
    """Base class for a provider-specific failure that's already a clean,
    actionable message (auth/rate-limit/gated/network) -- never a raw
    traceback. Every provider's error type subclasses this so signal-failure
    handling (`signals._SIGNAL_ERRORS`) and `server.handle_errors` work the
    same regardless of which backend raised it."""


@runtime_checkable
class Provider(Protocol):
    """The subset of a backend's surface the rest of re-com relies on.

    Matches `ytmusicapi.YTMusic`'s shape (the reference implementation,
    `YTMusicClient`) since that's the shape `signals.py` and `server.py`
    already assume. `videoId` in every returned track dict is this
    interface's track-identity field name for any backend, not literally a
    YouTube video id -- see `spotify_client.py` for why that field is kept
    rather than renamed.
    """

    def search(self, query: str, filter: str | None = None, limit: int = 20) -> list[dict[str, Any]]: ...

    def get_library_playlists(self, limit: int | None = 25) -> list[dict[str, Any]]: ...

    def get_playlist(self, playlist_id: str, limit: int | None = 100) -> dict[str, Any]: ...

    def get_watch_playlist(
        self, videoId: str | None = None, limit: int = 25, radio: bool = False
    ) -> dict[str, Any]: ...

    def get_song_related(self, browseId: str) -> list[dict[str, Any]]: ...

    def get_artist(self, channelId: str) -> dict[str, Any]: ...

    def get_history(self) -> list[dict[str, Any]]: ...
