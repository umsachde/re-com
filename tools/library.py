"""Body for server.py's refresh_library tool. See its counterpart in
server.py for the tool's user-facing contract (docstring); PLAN.md 7.9."""

from typing import Any

import server


def refresh_library(video_ids: list[str] | None = None) -> dict[str, Any]:
    """See server.refresh_library for the tool contract."""
    added = {v for v in (video_ids or []) if isinstance(v, str) and v}
    base = {
        "cache_path": str(server.CACHE_PATH),
        "ttl_seconds": server.CACHE_TTL,
        "served_ttl_seconds": server.SERVED_TTL,
    }

    if added:
        cached = server._read_cache()
        if cached is not None:
            ids, fetched_at = cached
            merged = ids | added
            server._write_cache(merged, fetched_at=fetched_at)
            return {**base, "rebuilt": False, "added": len(added - ids), "tracks_excluded": len(merged)}

    # No ids, or no usable cache to add them to: a full build. Ids passed in
    # are still unioned, in case the service hasn't surfaced the add yet.
    ids = server._library_video_ids(server._client(), force_refresh=True)
    if added - ids:
        ids |= added
        server._write_cache(ids)
    return {**base, "rebuilt": True, "added": len(added), "tracks_excluded": len(ids)}
