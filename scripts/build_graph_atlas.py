#!/usr/bin/env python3
"""Build the provider-neutral mood atlas from Deezer, then apply it.

Three stages, each independently re-runnable:

  crawl        Search Deezer for mood playlists and read their tracks into
               the shared graph cache. Resumable by (mood, query) -- a crawl
               this size will be interrupted, and starting over would be
               unacceptable.
  materialize  Collapse playlist membership into one mood vector per Deezer
               track. Seconds, and re-runnable after retuning
               moodspace.ANCHORS without re-crawling.
  propagate    Give this provider's library tracks those moods, through the
               cached id bridge, writing into the provider's own store.

Unlike scripts/build_atlas.py (which crawls YouTube Music's editorial mood
taxonomy and only works there), everything here is service-neutral: the crawl
and materialize stages write to the ONE shared graph cache that every backend
reads, so connecting a second service costs only the propagate stage.

Usage:
    python scripts/build_graph_atlas.py                # all three stages
    python scripts/build_graph_atlas.py --stage crawl --limit 20
    python scripts/build_graph_atlas.py --stage propagate
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graph_atlas  # noqa: E402
import graph_store  # noqa: E402
import store  # noqa: E402


def _crawl(graph_conn, limit, fresh):
    started = time.time()

    def progress(stats):
        done = stats["queries"]
        print(
            f"  [{done:4}] {stats['mood']:<10} {stats['query']:<28} "
            f"playlists={stats['playlists']:<5} tracks={stats['tracks']:<7} "
            f"{time.time() - started:6.0f}s",
            flush=True,
        )

    print("Crawling Deezer mood playlists...")
    stats = graph_atlas.crawl(
        graph_conn, resume=not fresh, limit=limit, on_progress=progress
    )
    print(
        f"\nCrawled {stats['queries']} queries "
        f"({stats['skipped']} already done, skipped): "
        f"{stats['playlists']} playlists, {stats['tracks']} memberships."
    )


def _materialize(graph_conn):
    print("Materializing mood vectors...")
    written = graph_atlas.materialize_moods(graph_conn)
    print(f"  {written} Deezer tracks now carry a graph-atlas mood.")


def _propagate(graph_conn, store_conn):
    rows = [
        {"video_id": vid, "title": (store.get_track(store_conn, vid) or {}).get("title"),
         "artists": (store.get_track(store_conn, vid) or {}).get("artists")}
        for vid in sorted(store.library_video_ids(store_conn))
    ]
    if not rows:
        print(
            "No library tracks in this provider's store yet -- run "
            "scripts/label_library.py first so there is something to label."
        )
        return

    print(f"Propagating graph moods onto {len(rows)} library tracks...")
    started = time.time()

    def progress(stats):
        total = stats["labeled"] + stats["unresolved"] + stats["no_mood"]
        if total % 25 == 0:
            print(
                f"  {total:5}/{len(rows)}  labeled={stats['labeled']:<5} "
                f"no_mood={stats['no_mood']:<5} unresolved={stats['unresolved']:<5} "
                f"{time.time() - started:6.0f}s",
                flush=True,
            )

    stats = graph_atlas.propagate_to_provider(
        store_conn, graph_conn, rows, on_progress=progress
    )
    print(
        f"\nLabeled {stats['labeled']}; {stats['no_mood']} resolved to Deezer but "
        f"had no atlas mood; {stats['unresolved']} did not resolve at all."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", choices=("crawl", "materialize", "propagate", "all"), default="all"
    )
    parser.add_argument("--limit", type=int, help="max queries to crawl this run")
    parser.add_argument(
        "--fresh", action="store_true", help="ignore the resume point and re-crawl"
    )
    args = parser.parse_args()

    graph_conn = graph_store.connect()

    if args.stage in ("crawl", "all"):
        _crawl(graph_conn, args.limit, args.fresh)
    if args.stage in ("materialize", "all"):
        _materialize(graph_conn)
    if args.stage in ("propagate", "all"):
        _propagate(graph_conn, store.connect())

    print("\nCoverage:")
    for key, value in graph_atlas.coverage(graph_conn).items():
        print(f"  {key:<20} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
