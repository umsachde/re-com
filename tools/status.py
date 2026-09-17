"""Body for server.py's index_status tool, and the two helpers only it uses.
See index_status's counterpart in server.py for the tool's user-facing
contract (docstring); PLAN.md 7.9."""

from typing import Any

import provider as provider_module
import server


def index_status() -> dict[str, Any]:
    """See server.index_status for the tool contract."""
    import judge
    import label
    import store as _s

    conn = server._store()
    _s.infer_implicit_feedback(conn)
    return {
        # Name the backend and its store: these numbers describe one
        # provider's index, and an empty one on a fresh backend is a real
        # answer rather than a bug.
        "provider": server.PROVIDER,
        "store": str(_s.DB_PATH),
        "mood_supported": server.PROVIDER in server.MOOD_PROVIDERS,
        "atlas": _s.atlas_stats(conn),
        "library": label.library_coverage(conn),
        "feedback": _s.feedback_stats(conn),
        "graph": _graph_status(),
        "maintenance": _maintenance_report(conn),
        "llm_labelling": {
            "available": judge.available(),
            "model": judge.MODEL,
            "hint": None if judge.available() else "Install with: pip install -e '.[llm]' and run 'ant auth login'.",
        },
    }


def _maintenance_report(conn: Any) -> dict[str, Any]:
    """Staleness and trend since the last `scripts/maintain.py` run (PLAN.md 7.4).

    A total on its own doesn't say whether the index is still growing or has
    stalled since crawling stopped mattering to whoever set this up. Diffing
    the live numbers against the snapshot the last maintenance run took makes
    that visible instead of silent, which is the whole point of §7.4's
    "staleness and trend rather than only totals".
    """
    import store as _s

    status = _s.maintenance_status(conn)
    if status["last_run_at"] is None:
        return {"last_run_at": None, "hint": "scripts/maintain.py has never run. See PLAN.md 7.4."}

    graph_coverage = None
    if server.GRAPH_ENABLED:
        try:
            import graph_atlas

            graph_coverage = graph_atlas.coverage(server._graph())
        except Exception:  # noqa: BLE001 - a status report must never fail the call
            graph_coverage = None

    current = _s.coverage_snapshot(conn, graph_coverage=graph_coverage)
    previous = status["snapshot"] or {}
    return {
        "last_run_at": status["last_run_at"],
        "stale_hours": status["stale_hours"],
        "trend_since_last_run": {k: round(v - previous[k], 4) for k, v in current.items() if k in previous},
        "last_run_stages": status["last_stages"],
    }


def _graph_status() -> dict[str, Any]:
    """What the music graph holds, for index_status.

    Reported alongside the mood index for the same reason that exists: on a
    backend whose native signals are gone, the graph is where every
    recommendation now comes from, so an empty graph is the difference between
    good results and none. Its capabilities are named too, because "this
    backend supplies no native signals" is the single most useful thing to
    know when results look thin.
    """
    if not server.GRAPH_ENABLED:
        return {"enabled": False}

    import graph_atlas
    import graph_store

    try:
        conn = server._graph()
        caps = sorted(provider_module.capabilities_of(server._client()))
        return {
            "enabled": True,
            "path": str(graph_store.DB_PATH),
            "native_signals": caps or None,
            "cache": graph_store.stats(conn),
            "atlas": graph_atlas.coverage(conn),
        }
    except Exception as e:  # noqa: BLE001 - a status report must never fail the call
        return {"enabled": True, "error": f"{type(e).__name__}: {e}"}
