#!/usr/bin/env python3
"""Give the listener's library mood labels.

Runs the layers cheapest-first, so each pass only pays for what the previous
one couldn't cover:

  1. sync      mirror Liked Music and every playlist into the store
  2. atlas     materialise mood vectors from crawled playlist membership
  3. artist    propagate an artist's average mood to their unlabelled songs
  4. claude    read the lyrics of whatever is still unlabelled (optional)

Steps 1-3 need no credentials beyond YouTube Music auth. Step 4 needs the
`anthropic` package and Anthropic credentials, and is skipped with a clear note
when they're absent -- it is also the only step that closes the gap on the
non-English catalogue, so the coverage report says what was missed.

    python scripts/label_library.py                # steps 1-3
    python scripts/label_library.py --claude       # all four
    python scripts/label_library.py --claude --max 200
    python scripts/label_library.py --report       # coverage only
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atlas  # noqa: E402
import judge  # noqa: E402
import label  # noqa: E402
import lyrics as lyrics_mod  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--claude", action="store_true", help="label remaining songs by reading their lyrics")
    parser.add_argument("--max", type=int, default=None, help="cap how many songs the Claude pass labels")
    parser.add_argument("--report", action="store_true", help="print coverage and exit")
    parser.add_argument("--skip-sync", action="store_true", help="don't re-fetch the library")
    args = parser.parse_args()

    conn = store.connect()

    if args.report:
        _report(conn)
        return 0

    # The Provider seam, not ytmusicapi: library sync must work on whichever
    # backend RECOM_PROVIDER selects, or mood stays YouTube-only.
    yt = server._client()

    if not args.skip_sync:
        started = time.time()
        print("1/4 syncing library...", flush=True)
        synced = label.sync_library(conn, yt)
        print(f"    {synced['unique_tracks']:,} tracks across {synced['playlists']} playlists "
              f"({time.time() - started:.1f}s)", flush=True)

    print("2/4 materialising atlas moods...", flush=True)
    print(f"    {atlas.materialize_moods(conn):,} songs placed from mood-playlist membership", flush=True)

    print("3/4 propagating by artist...", flush=True)
    print(f"    {label.propagate_by_artist(conn):,} songs given their artist's profile", flush=True)

    if args.claude:
        print("4/4 labelling the rest with Claude...", flush=True)
        _claude_pass(conn, yt, args.max)
    else:
        print("4/4 skipped (pass --claude to read lyrics for what's still unlabelled)", flush=True)

    _report(conn)
    return 0


def _claude_pass(conn, yt, cap: int | None) -> None:
    if not judge.available():
        print("    unavailable: install with `pip install -e '.[llm]'` and run `ant auth login`.")
        print("    Without it, the non-English catalogue stays largely unlabelled — see PLAN_V2.md.")
        return

    library = store.library_video_ids(conn)
    labelled = label.resolve_many(conn, library)
    pending = [
        v for v in library
        if v not in labelled or labelled[v]["source"] == "artist"
    ]
    if cap:
        pending = pending[:cap]
    if not pending:
        print("    nothing left to label.")
        return

    print(f"    {len(pending):,} songs to label with {judge.MODEL}", flush=True)

    prepared = []
    for video_id in pending:
        track = store.get_track(conn, video_id) or {}
        text = lyrics_mod.get_or_fetch(conn, yt, video_id)
        prepared.append(
            {
                "video_id": video_id,
                "title": track.get("title"),
                "artists": track.get("artists"),
                "moods": sorted(store.atlas_mood_counts(conn, video_id)),
                "playlists": [m["playlist_title"] for m in store.atlas_moods_for(conn, video_id) if m["playlist_title"]],
                "lyrics": lyrics_mod.excerpt(text),
            }
        )

    written = 0
    for batch in judge.batches(prepared):
        try:
            results = judge.label_batch(batch)
        except Exception as e:  # noqa: BLE001 - one bad batch shouldn't lose the run
            print(f"    batch failed ({type(e).__name__}); continuing", flush=True)
            continue
        for video_id, (vector, confidence) in results.items():
            store.put_track_mood(conn, video_id, "llm", vector, confidence)
            written += 1
        print(f"    {written:,}/{len(prepared):,} labelled", flush=True)

    print(f"    {written:,} songs labelled by Claude", flush=True)


def _report(conn) -> None:
    coverage = label.library_coverage(conn)
    print(f"\nlibrary          : {coverage['library']:,} tracks")
    print(f"with a mood      : {coverage['labelled']:,}  ({coverage['coverage'] * 100:.1f}%)")
    for source, n in coverage["by_source"].items():
        print(f"    {source:8s} {n:6,d}")
    if coverage["coverage"] < 0.35:
        print("\nCoverage is low. Recommendations will rank mostly on signal agreement")
        print("rather than on mood. Finish the atlas crawl (scripts/build_atlas.py)")
        print("and/or run this again with --claude.")


if __name__ == "__main__":
    raise SystemExit(main())
