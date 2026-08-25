#!/usr/bin/env python3
"""Crawl YouTube Music's mood playlists into the local store.

Resumable: re-running picks up where the last run stopped and retries only the
playlists that failed. Expect ~35 minutes for a cold full crawl.

    python scripts/build_atlas.py            # full crawl, then materialize
    python scripts/build_atlas.py --limit 40 # short trial run
    python scripts/build_atlas.py --status   # report progress, crawl nothing

**Deliberately still on ytmusicapi, unlike the other offline scripts.** v6
moved `label_library.py`, `snapshot_history.py` and `quality_check.py` onto the
Provider seam so they work on any backend. This one cannot follow, and should
not be "fixed" to: it crawls YouTube Music's own editorial "Moods & moments"
taxonomy, which no other service has. It stays as the richer YouTube-native
mood source, ranked above the neutral one in `label.SOURCE_PRIORITY`.

The portable equivalent is `scripts/build_graph_atlas.py`, which builds the
same kind of evidence from Deezer for every backend.
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atlas  # noqa: E402
import store  # noqa: E402
from ytmusicapi import YTMusic  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="crawl at most N playlists this run")
    parser.add_argument("--status", action="store_true", help="print progress and exit")
    parser.add_argument("--materialize-only", action="store_true", help="recompute vectors from existing membership")
    args = parser.parse_args()

    conn = store.connect()

    if args.status:
        _print_stats(store.atlas_stats(conn))
        return 0

    if args.materialize_only:
        print(f"materialized {atlas.materialize_moods(conn):,} atlas mood vectors")
        return 0

    auth = os.environ.get("RECOM_AUTH_PATH", "headers_auth.json")
    if not Path(auth).exists():
        print(f"error: {auth} not found. Run scripts/setup_auth_from_file.py first.", file=sys.stderr)
        return 1

    yt = YTMusic(auth)
    started = time.time()

    def progress(p):
        if p["index"] % 25 == 0 or p["status"] != "ok":
            done, total = p["index"], p["pending"]
            rate = (time.time() - started) / max(done, 1)
            eta = (total - done) * rate / 60
            flag = "" if p["status"] == "ok" else f"  [{p['status']}]"
            print(
                f"  {done:5d}/{total:<5d} {p['mood']:10s} {p['tracks']:7,d} tracks  "
                f"eta {eta:5.1f}m  {(p['title'] or '')[:38]}{flag}",
                flush=True,
            )

    print("enumerating mood playlists...", flush=True)
    stats = atlas.crawl(yt, conn, on_progress=progress, limit=args.limit)
    print(
        f"\ncrawl done in {(time.time() - started) / 60:.1f}m: "
        f"{stats['ok']:,} ok, {stats['failed']:,} failed, "
        f"{stats['already_crawled']:,} already had, {stats['deferred']:,} deferred, "
        f"{stats['tracks']:,} memberships",
        flush=True,
    )

    print(f"materialized {atlas.materialize_moods(conn):,} atlas mood vectors", flush=True)
    _print_stats(store.atlas_stats(conn))
    return 0


def _print_stats(s):
    print(f"\nplaylists crawled : {s['playlists_ok']:,} ok / {s['playlists_crawled']:,} attempted")
    print(f"unique tracks     : {s['unique_tracks']:,}")
    print(f"memberships       : {s['memberships']:,}")
    for mood, n in s["moods"].items():
        print(f"    {mood:10s} {n:7,d} tracks")


if __name__ == "__main__":
    raise SystemExit(main())
