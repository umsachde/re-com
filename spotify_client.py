"""Talks to the sibling `spotify-mcp` server for every Spotify call.

re-com holds no Spotify credentials of its own -- no client secret, no OAuth
token, no login flow. All of that lives in `spotify-mcp`; this module spawns
it as an MCP server subprocess (the same way `ytmusic_client.YTMusicClient`
talks to `ytmusic-mcp`, and the same way any MCP client would) and exposes
the `provider.Provider` interface on top of it.

Spotify's Web API doesn't have YouTube Music's watch-playlist/related/artist
shapes -- there's no per-track "radio" queue or "related content" browse id.
Rather than growing a second, backend-agnostic candidate-generation contract
(which would mean rewriting `signals.py`'s proven multi-signal logic and its
whole test suite), this client *builds* those shapes out of Spotify's actual
endpoints:

  get_watch_playlist  -> the seed track itself, plus Spotify's
                         /recommendations for it (the "radio" analog).
  get_song_related    -> the seed's artist's related artists' top tracks
                         (Spotify has no per-track related-content feed).
  get_artist          -> the artist's top tracks (Spotify's artist pages
                         don't expose a full catalog playlist the way
                         YouTube Music's channel "Songs" tab does -- top
                         tracks, capped at ~10, is the deepest available)
                         plus its related artists.

`videoId` is kept as the track-identity field name throughout, even though
it now holds a Spotify track id -- see provider.py for why the field isn't
renamed per-backend.

Spotify has been restricting several discovery endpoints (recommendations,
related-artists) for API apps registered after Nov 2024; if your app doesn't
have access, those signals simply come back empty rather than failing the
whole seed -- same graceful-degradation behavior YouTube Music signals get
when one of them errors.

Configured via RECOM_SPOTIFY_MCP_COMMAND (interpreter) and
RECOM_SPOTIFY_MCP_ARGS (space-separated args, typically just spotify-mcp's
server.py path) -- the same command/args split `claude mcp add` uses to
register spotify-mcp itself.

spotify-mcp reads its own credentials (SPOTIFY_CLIENT_ID/SECRET) and token
cache location (SPOTIFY_CACHE_PATH) from its environment, so those are
forwarded from re-com's environment into the subprocess -- see
`_subprocess_env`. Without that forwarding, MCP's stdio client hands the
child only a minimal safe environment (PATH/HOME/USER/...), and spotify-mcp
fails every call with "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be
set" even though re-com itself was configured with them.
"""

import asyncio
import atexit
import os
import shlex
import threading
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from provider import ProviderError

_CONNECT_TIMEOUT = 30
_CALL_TIMEOUT = 60
# How many of the seed artist's related artists to pull top tracks from for
# the "related" signal. Independent of signals._RELATED_ARTISTS_TO_EXPAND,
# which is a separate expansion step inside the "artist" signal -- this
# client doesn't reach into signals.py's internals.
_RELATED_ARTISTS_FOR_SIGNAL = 5

CONFIG_HELP = (
    "spotify-mcp is not configured. Set RECOM_SPOTIFY_MCP_COMMAND (its interpreter) "
    "and RECOM_SPOTIFY_MCP_ARGS (its server.py path) so re-com can reach Spotify "
    "through it -- see README."
)


class SpotifyMCPError(ProviderError):
    """A call to spotify-mcp failed; the message is already clear and actionable
    (spotify-mcp translates auth/rate-limit/restricted-endpoint/network failures
    itself)."""


def _track_from_spotify(item: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a Spotify track object into the shape signals.py/server.py
    expect (the same shape ytmusicapi's track dicts already have)."""
    if not item:
        return {}
    artists = [
        {"name": a.get("name"), "id": a.get("id")}
        for a in (item.get("artists") or [])
        if a.get("name")
    ]
    album = item.get("album")
    album_name = album.get("name") if isinstance(album, dict) else album
    return {
        "videoId": item.get("id"),
        "title": item.get("name"),
        "artists": artists,
        "album": {"name": album_name} if album_name else None,
    }


def _artist_from_spotify(item: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a Spotify artist object into the shape server.py's
    `_resolve_artist` expects from a ytmusicapi artist search result."""
    if not item:
        return {}
    return {"artist": item.get("name"), "browseId": item.get("id")}


def _subprocess_env() -> dict[str, str]:
    """MCP's minimal default child environment plus spotify-mcp's own config.

    `stdio_client` deliberately doesn't inherit the parent's whole
    environment, so anything spotify-mcp needs has to be passed explicitly.
    """
    env = get_default_environment()
    env.update(
        {k: v for k, v in os.environ.items() if k.startswith("SPOTIFY_")}
    )
    return env


class SpotifyClient:
    """Synchronous facade over a persistent spotify-mcp subprocess.

    Same connect/call/close plumbing as YTMusicClient (see ytmusic_client.py)
    -- one dedicated background thread runs the asyncio event loop the MCP
    session needs; every public method blocks the caller's thread on the
    result.
    """

    def __init__(self, command: str | None = None, args: list[str] | None = None):
        command = command or os.environ.get("RECOM_SPOTIFY_MCP_COMMAND")
        if args is None:
            raw_args = os.environ.get("RECOM_SPOTIFY_MCP_ARGS")
            args = shlex.split(raw_args) if raw_args else None
        if not command or not args:
            raise RuntimeError(CONFIG_HELP)
        self._command = command
        self._args = args

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

        fut = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        try:
            fut.result(timeout=_CONNECT_TIMEOUT)
        except Exception as e:
            self._shutdown_loop()
            raise RuntimeError(f"Couldn't start spotify-mcp: {e}") from e

        atexit.register(self.close)

    async def _connect(self) -> None:
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=self._command, args=self._args, env=_subprocess_env()
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session

    def _call(self, tool: str, *, unwrap: bool = True, **arguments: Any) -> Any:
        fut = asyncio.run_coroutine_threadsafe(self._call_async(tool, arguments, unwrap), self._loop)
        return fut.result(timeout=_CALL_TIMEOUT)

    async def _call_async(self, tool: str, arguments: dict[str, Any], unwrap: bool) -> Any:
        assert self._session is not None
        result = await self._session.call_tool(tool, arguments)
        if result.is_error:
            text = "".join(getattr(block, "text", "") for block in result.content)
            raise SpotifyMCPError(text or f"spotify-mcp's {tool} failed")
        content = result.structured_content
        if unwrap and isinstance(content, dict) and "result" in content:
            return content["result"]
        return content

    def close(self) -> None:
        if self._stack is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._stack.aclose(), self._loop)
        try:
            fut.result(timeout=10)
        except Exception:
            pass
        self._shutdown_loop()

    def _shutdown_loop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    # --- provider.Provider surface ------------------------------------------

    def search(self, query: str, filter: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        kind = "artist" if filter == "artists" else "track"
        raw = self._call("search_music", query=query, filter=kind, limit=limit)
        if kind == "artist":
            return [_artist_from_spotify(a) for a in raw]
        return [_track_from_spotify(t) for t in raw]

    def get_library_playlists(self, limit: int | None = 25) -> list[dict[str, Any]]:
        raw = self._call("get_playlists", limit=limit)
        return [{"playlistId": p.get("id"), "title": p.get("name")} for p in raw]

    def get_playlist(self, playlistId: str, limit: int | None = 100) -> dict[str, Any]:
        # "LM" mirrors ytmusicapi's magic id for Liked Music -- Spotify's
        # equivalent (Saved Tracks) isn't a playlist at all, so it's routed
        # to a different spotify-mcp tool but kept under the same alias to
        # match Provider's contract (see _liked_video_ids in server.py).
        if playlistId == "LM":
            raw = self._call("get_saved_tracks", limit=limit)
        else:
            raw = self._call("get_playlist_tracks", playlist_id=playlistId, limit=limit)
        return {"tracks": [_track_from_spotify(t) for t in raw]}

    def get_watch_playlist(
        self, videoId: str | None = None, limit: int = 25, radio: bool = False
    ) -> dict[str, Any]:
        tracks: list[dict[str, Any]] = []
        if videoId:
            try:
                seed = _track_from_spotify(self._call("get_track", track_id=videoId))
                if seed.get("videoId"):
                    tracks.append(seed)
            except SpotifyMCPError:
                pass
        try:
            recs = self._call("get_recommendations", seed_track_id=videoId, limit=limit)
            tracks.extend(_track_from_spotify(t) for t in recs)
        except SpotifyMCPError:
            # /recommendations is one of the endpoints Spotify restricts for
            # newer API apps -- treated as a signal that's simply unavailable,
            # not a fatal error (same as any other ProviderError signal skip).
            pass
        return {"tracks": tracks, "related": videoId}

    def get_song_related(self, browseId: str) -> list[dict[str, Any]]:
        # browseId here is the seed track id itself -- see get_watch_playlist
        # above, which hands it back as "related" since Spotify has no
        # separate browse-id concept for a track's related content.
        try:
            seed = self._call("get_track", track_id=browseId)
        except SpotifyMCPError:
            return []
        artist_id = ((seed.get("artists") or [{}])[0]).get("id")
        if not artist_id:
            return []
        try:
            related = self._call("get_related_artists", artist_id=artist_id)
        except SpotifyMCPError:
            return []

        contents = []
        for artist in related[:_RELATED_ARTISTS_FOR_SIGNAL]:
            artist_id = artist.get("id")
            if not artist_id:
                continue
            try:
                top = self._call("get_artist_top_tracks", artist_id=artist_id)
            except SpotifyMCPError:
                continue
            contents.extend(_track_from_spotify(t) for t in top)
        return [{"contents": contents}]

    def get_artist(self, channelId: str) -> dict[str, Any]:
        try:
            top = self._call("get_artist_top_tracks", artist_id=channelId)
        except SpotifyMCPError:
            top = []
        try:
            related = self._call("get_related_artists", artist_id=channelId)
        except SpotifyMCPError:
            related = []
        return {
            # No browseId: Spotify has no separate "full catalog" playlist to
            # follow the way YouTube Music's channel "Songs" browseId does --
            # top tracks (capped ~10 by Spotify) is the deepest available, so
            # server._artist_song_catalog falls back to "results" directly.
            "songs": {"browseId": None, "results": [_track_from_spotify(t) for t in top]},
            "related": {"results": [{"browseId": a["id"]} for a in related if a.get("id")]},
        }

    def get_history(self) -> list[dict[str, Any]]:
        raw = self._call("get_recently_played", limit=50)
        return [_track_from_spotify(item.get("track") or item) for item in raw]
