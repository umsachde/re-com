#!/usr/bin/env python3
"""Top up every index re-com reads from, in the order that matters, on a schedule.

PLAN.md 7.4: coverage is the quality ceiling (see graph.py, brainz.py, and
§3's measurements) and it is crawl-bound -- growing it means crawling more,
not resolving better. Every crawl already resumes correctly on its own
(build_atlas.py, build_graph_atlas.py and build_tempo.py all skip what they've
already attempted), so "continuous" only needed one thing this script adds:
something that calls them all, in order, on its own, and says honestly what
it found.

Stages, cheapest and most load-bearing first:

  1. library sync + atlas materialize + artist propagation  (label_library.py's
     steps 1-3 -- provider-neutral, always runs)
  2. YouTube's editorial mood atlas and genre pages           (YouTube only --
     the richer, YouTube-native mood source; skipped elsewhere)
  3. tempo backfill against Deezer                            (provider-neutral)
  4. the shared graph atlas: crawl, materialize, propagate    (provider-neutral,
     shared across every backend)

Each stage is independent and caught on its own: one failing (a 503, a
missing token, no auth file) must not stop the ones after it, the same
contract `graph.neighbours` already holds its sources to. What ran, what was
skipped and why, and what changed since the last run are recorded so
`index_status()` can report staleness and trend rather than only a total.

    python scripts/maintain.py                 # bounded top-up, all stages
    python scripts/maintain.py --full           # no per-stage limits (slow)
    python scripts/maintain.py --status         # report only, crawl nothing

Worth running on a schedule, e.g. nightly:

    0 3 * * * cd /path/to/re-com && .venv/bin/python scripts/maintain.py
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atlas  # noqa: E402
import graph_atlas  # noqa: E402
import graph_store  # noqa: E402
import label  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
import taxonomy  # noqa: E402
import tempo  # noqa: E402

# Bounded by default so a scheduled run stays short; a cold install still
# converges, just over several runs instead of one 45-minute one. --full lifts
# every cap for a deliberate one-shot catch-up.
DEFAULT_ATLAS_LIMIT = 60
DEFAULT_GENRE_PLAYLISTS = 12
DEFAULT_TEMPO_LIMIT = 400
DEFAULT_GRAPH_LIMIT = 40


def _stage(fn) -> dict:
    """Run one stage, never letting it take the rest of the script down with it.

    Mirrors the contract `graph.py`'s second and third sources are held to:
    a source that can't answer degrades silently, it doesn't fail the whole
    pass. A maintenance job is exactly the same shape -- one bad stage should
    cost that stage's coverage, not tonight's YouTube atlas top-up.
    """
    started = time.time()
    try:
        result = fn()
        return {"status": "ok", "seconds": round(time.time() - started, 1), **(result or {})}
    except Exception as e:  # noqa: BLE001 - a maintenance run must survive one bad stage
        return {"status": "error", "seconds": round(time.time() - started, 1), "error": f"{type(e).__name__}: {e}"}


def _youtube_auth_available() -> Path | None:
    auth = Path(os.environ.get("RECOM_AUTH_PATH", "headers_auth.json"))
    return auth if auth.exists() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="no per-stage limits (a cold install's first run)")
    parser.add_argument("--status", action="store_true", help="report staleness and last run, crawl nothing")
    args = parser.parse_args()

    conn = store.connect()

    if args.status:
        _report_status(conn)
        return 0

    atlas_limit = None if args.full else DEFAULT_ATLAS_LIMIT
    tempo_limit = None if args.full else DEFAULT_TEMPO_LIMIT
    graph_limit = None if args.full else DEFAULT_GRAPH_LIMIT

    stages: dict[str, dict] = {}

    print("1/4 library sync + atlas materialize + artist propagation...", flush=True)
    # The client is built inside the stage: an unconfigured or dead provider
    # subprocess costs the sync, not the tempo and graph stages that don't need it.
    stages["library_sync"] = _stage(lambda: label.sync_library(conn, server._client()))
    stages["atlas_materialize"] = _stage(lambda: {"placed": atlas.materialize_moods(conn)})
    stages["artist_propagate"] = _stage(lambda: {"propagated": label.propagate_by_artist(conn)})
    for name in ("library_sync", "atlas_materialize", "artist_propagate"):
        print(f"    {name}: {stages[name]}", flush=True)

    auth_path = _youtube_auth_available()
    if server.PROVIDER == "youtube" and auth_path:
        print("2/4 YouTube editorial mood atlas + genre pages...", flush=True)
        yt_holder: list = []

        def _yt():
            if not yt_holder:
                from ytmusicapi import YTMusic  # local: keeps this optional for non-YouTube installs

                yt_holder.append(YTMusic(str(auth_path)))
            return yt_holder[0]

        stages["youtube_atlas"] = _stage(lambda: atlas.crawl(_yt(), conn, limit=atlas_limit))
        stages["youtube_atlas_materialize"] = _stage(lambda: {"placed": atlas.materialize_moods(conn)})
        stages["youtube_genres"] = _stage(
            lambda: taxonomy.crawl_genres(_yt(), conn, playlists_per_genre=DEFAULT_GENRE_PLAYLISTS)
        )
        for name in ("youtube_atlas", "youtube_atlas_materialize", "youtube_genres"):
            print(f"    {name}: {stages[name]}", flush=True)
    else:
        reason = "provider is not youtube" if server.PROVIDER != "youtube" else "no headers_auth.json (RECOM_AUTH_PATH)"
        print(f"2/4 skipped ({reason})", flush=True)
        stages["youtube_atlas"] = {"status": "skipped", "reason": reason}

    print("3/4 tempo backfill...", flush=True)

    def _tempo() -> dict:
        # Filter to never-attempted tracks *before* the limit. Truncating first
        # re-checked the same 400 cached rows every bounded run and never
        # reached the rest of the library -- seen live as cached=400, resolved=0.
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT t.video_id, t.title, t.artists FROM track t "
                "JOIN library_track l USING (video_id) "
                "LEFT JOIN track_tempo tt ON tt.video_id = t.video_id "
                "WHERE t.title IS NOT NULL AND tt.video_id IS NULL "
                "GROUP BY t.video_id"
            )
        ]
        if tempo_limit is not None:
            rows = rows[:tempo_limit]
        return {"pending_before": len(rows), **tempo.backfill(conn, rows)}

    stages["tempo"] = _stage(_tempo)
    print(f"    tempo: {stages['tempo']}", flush=True)

    graph_coverage = None
    if server.GRAPH_ENABLED:
        print("4/4 shared graph atlas: crawl, materialize, propagate...", flush=True)
        graph_conns: list = []

        def _graph_conn():
            if not graph_conns:
                graph_conns.append(graph_store.connect())
            return graph_conns[0]

        stages["graph_crawl"] = _stage(lambda: graph_atlas.crawl(_graph_conn(), limit=graph_limit))
        stages["graph_materialize"] = _stage(lambda: {"placed": graph_atlas.materialize_moods(_graph_conn())})

        def _graph_propagate() -> dict:
            rows = []
            for vid in sorted(store.library_video_ids(conn)):
                track = store.get_track(conn, vid) or {}
                rows.append({"video_id": vid, "title": track.get("title"), "artists": track.get("artists")})
            return graph_atlas.propagate_to_provider(conn, _graph_conn(), rows)

        stages["graph_propagate"] = _stage(_graph_propagate)
        for name in ("graph_crawl", "graph_materialize", "graph_propagate"):
            print(f"    {name}: {stages[name]}", flush=True)
        try:
            graph_coverage = graph_atlas.coverage(_graph_conn())
        except Exception as e:  # noqa: BLE001 - the run's record matters more than the graph numbers
            print(f"    graph coverage unavailable: {type(e).__name__}: {e}", flush=True)
    else:
        print("4/4 skipped (graph disabled: RECOM_GRAPH=0)", flush=True)
        stages["graph_crawl"] = {"status": "skipped", "reason": "RECOM_GRAPH=0"}

    # Captured before recording, not after: `record_maintenance_run` below
    # overwrites the very snapshot this run needs to diff against, so reading
    # it back afterwards compares the run against itself and reports a flat
    # zero trend regardless of what actually changed. Caught on the first
    # live run, where every field misleadingly read +0.0000.
    previous_snapshot = store.maintenance_status(conn)["snapshot"] or {}

    snapshot = store.coverage_snapshot(conn, graph_coverage=graph_coverage)
    store.record_maintenance_run(conn, snapshot, stages)

    failed = [name for name, result in stages.items() if result.get("status") == "error"]
    print(f"\ndone. {len(stages) - len(failed)}/{len(stages)} stages ok" + (f", {len(failed)} failed: {failed}" if failed else ""))
    _print_trend(snapshot, previous_snapshot)
    return 1 if failed else 0


def _print_trend(current: dict, previous: dict) -> None:
    print("\ncoverage now vs. previous run:" if previous else "\ncoverage now (first run, nothing to compare against):")
    for key, value in sorted(current.items()):
        prior = previous.get(key)
        delta = f"  ({value - prior:+.4f})" if prior is not None else "  (new)"
        print(f"    {key:24s} {value:10.4f}{delta}")


def _report_status(conn) -> None:
    status = store.maintenance_status(conn)
    if status["last_run_at"] is None:
        print("\nnever run before.")
        return

    print(f"\nlast run    : {status['stale_hours']:.1f}h ago")
    graph_coverage = graph_atlas.coverage(graph_store.connect()) if server.GRAPH_ENABLED else None
    current = store.coverage_snapshot(conn, graph_coverage=graph_coverage)
    _print_trend(current, status["snapshot"] or {})


if __name__ == "__main__":
    raise SystemExit(main())
