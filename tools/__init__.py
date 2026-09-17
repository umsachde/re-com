"""Tool-body implementations for server.py's MCP tools (PLAN.md 7.9).

server.py owns tool *registration* (`@mcp.tool()`), the docstrings that are
each tool's user-facing contract, and the shared infrastructure every tool
body calls into (`_client`, `_store`, `_graph`, `_library_video_ids`, the
signals.py re-exports, ...). The modules here own the orchestration --
filter, store, resolve -- that used to sit inline in each `@mcp.tool()`
function.

Each module does `import server` at its own top level rather than
`from server import X` for the functions/state it calls: `server`'s
attributes are looked up fresh on every call, which is what lets tests
`monkeypatch.setattr(server, "_client", ...)` and have it take effect here
too. A `from server import X` would bind a stale reference at import time and
silently stop seeing test patches.

Only ever imported lazily, inside a `server.py` tool wrapper's body -- never
at server.py's own module level -- so `import server` here never races
server.py's own initialization.
"""
