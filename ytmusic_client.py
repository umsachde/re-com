"""Talks to the sibling `ytmusic-mcp` server for every YouTube Music call.

re-com holds no YouTube Music credentials of its own -- no auth file, no
header-scraping, no login flow. All of that lives in `ytmusic-mcp`; this
module spawns it as an MCP server subprocess (the same way any MCP client
would) and exposes just enough of its tools, under the same method names
`ytmusicapi.YTMusic` used to expose, that the rest of re-com (signals.py,
recommend.py, sense.py, server.py's library-cache building, ...) didn't need
to change at all -- it was already only calling get_watch_playlist,
get_song_related, get_artist, get_playlist, get_library_playlists, search,
and get_history on whatever `_client()` handed back.

Configured via RECOM_YTMUSIC_MCP_COMMAND (interpreter) and
RECOM_YTMUSIC_MCP_ARGS (space-separated args, typically just ytmusic-mcp's
server.py path) -- the same command/args split `claude mcp add` uses to
register ytmusic-mcp itself.

ytmusic-mcp reads its auth headers path (YTMUSIC_AUTH_PATH) from its
environment, so that's forwarded from re-com's environment into the
subprocess -- see `_subprocess_env`. Without that forwarding, MCP's stdio
client hands the child only a minimal safe environment
(PATH/HOME/USER/...), and ytmusic-mcp fails with an invalid-auth error even
though re-com itself was configured with the path.
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

CONFIG_HELP = (
    "ytmusic-mcp is not configured. Set RECOM_YTMUSIC_MCP_COMMAND (its interpreter) "
    "and RECOM_YTMUSIC_MCP_ARGS (its server.py path) so re-com can reach YouTube "
    "Music through it -- see README."
)


def _subprocess_env() -> dict[str, str]:
    """MCP's minimal default child environment plus ytmusic-mcp's own config.

    `stdio_client` deliberately doesn't inherit the parent's whole
    environment, so anything ytmusic-mcp needs has to be passed explicitly.
    """
    env = get_default_environment()
    env.update(
        {k: v for k, v in os.environ.items() if k.startswith("YTMUSIC_")}
    )
    return env


class YTMusicMCPError(ProviderError):
    """A call to ytmusic-mcp failed; the message is already clear and actionable
    (ytmusic-mcp translates auth/rate-limit/gated/network failures itself)."""


class YTMusicClient:
    """Synchronous facade over a persistent ytmusic-mcp subprocess.

    One dedicated background thread runs the asyncio event loop the MCP
    session needs; every public method blocks that caller's thread on the
    result, so the rest of re-com -- entirely synchronous -- doesn't need to
    know MCP-client calls are async under the hood.
    """

    def __init__(self, command: str | None = None, args: list[str] | None = None):
        command = command or os.environ.get("RECOM_YTMUSIC_MCP_COMMAND")
        if args is None:
            raw_args = os.environ.get("RECOM_YTMUSIC_MCP_ARGS")
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
            raise RuntimeError(f"Couldn't start ytmusic-mcp: {e}") from e

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
            raise YTMusicMCPError(text or f"ytmusic-mcp's {tool} failed")
        content = result.structured_content
        # ytmusic-mcp's tools that return a list or a str are wrapped by the
        # MCP framework as {"result": ...} (structured content must be a JSON
        # object on some protocol versions); tools returning dict[str, Any]
        # (get_watch_playlist, get_artist) are not.
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

    # --- ytmusicapi.YTMusic-compatible surface ------------------------------
    # Every method below matches the subset of YTMusic's signature/return
    # shape that the rest of re-com already relies on.

    def search(self, query: str, filter: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self._call("search_music", query=query, filter=filter, limit=limit)

    def get_library_playlists(self, limit: int | None = 25) -> list[dict[str, Any]]:
        return self._call("get_playlists", limit=limit)

    def get_playlist(self, playlistId: str, limit: int | None = 100) -> dict[str, Any]:
        tracks = self._call("get_playlist_tracks", playlist_id=playlistId, limit=limit)
        return {"tracks": tracks}

    def get_watch_playlist(
        self, videoId: str | None = None, limit: int = 25, radio: bool = False
    ) -> dict[str, Any]:
        return self._call("get_watch_playlist", unwrap=False, video_id=videoId, limit=limit, radio=radio)

    def get_song_related(self, browseId: str) -> list[dict[str, Any]]:
        return self._call("get_song_related", browse_id=browseId)

    def get_artist(self, channelId: str) -> dict[str, Any]:
        return self._call("get_artist", unwrap=False, browse_id=channelId)

    def get_history(self) -> list[dict[str, Any]]:
        return self._call("get_history")
