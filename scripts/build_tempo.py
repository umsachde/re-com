#!/usr/bin/env python3
"""Resolve BPM for your library via Deezer's public API.

No API key and no auth required. Roughly 0.4s per song, and results (including
"Deezer has no tempo for this") are cached permanently, so this only ever pays
for songs it hasn't seen.

Coverage is uneven and worth knowing before you rely on it. Measured on this
library: 6/6 on Pop, 4/6 on Rock and Reggae, but 1/6 on Punjabi and Bollywood.
The misses are genuine -- the songs are on Deezer, they simply have no tempo
analysis -- so recommendations never drop a song for having unknown BPM.

    python scripts/build_tempo.py              # whole library
    python scripts/build_tempo.py --max 200
    python scripts/build_tempo.py --status
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402
import tempo  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max", type=int, default=None, help="stop after N songs")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    conn = store.connect()
    if args.status:
        _report(conn)
        return 0

    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT t.video_id, t.title, t.artists FROM track t "
            "JOIN library_track l USING (video_id) WHERE t.title IS NOT NULL "
            "GROUP BY t.video_id"
        )
    ]
    if not rows:
        print("error: no library recorded. Run scripts/label_library.py first.", file=sys.stderr)
        return 1
    if args.max:
        rows = rows[: args.max]

    print(f"resolving tempo for {len(rows):,} songs (~{len(rows) * 0.4 / 60:.0f}m)...", flush=True)
    started = time.time()

    def progress(p):
        if p["index"] % 50 == 0:
            print(f"  {p['index']:5d}/{len(rows):<5d} {p['resolved']:5d} with BPM  "
                  f"{p['no_bpm']:5d} no tempo  {p['no_match']:4d} unmatched  "
                  f"{p['unavailable']:4d} unavailable (retried next run)", flush=True)

    stats = tempo.backfill(conn, rows, on_progress=progress)
    print(f"\ndone in {(time.time() - started) / 60:.1f}m: {stats}")
    _report(conn)
    return 0


def _report(conn) -> None:
    stats = store.tempo_stats(conn)
    print(f"\ntempo attempted : {stats['attempted']:,}")
    print(f"with a BPM      : {stats['with_bpm']:,}  ({stats['coverage'] * 100:.1f}%)")
    for status, n in sorted(stats["by_status"].items(), key=lambda kv: -kv[1]):
        print(f"    {status:10s} {n:6,d}")

    rows = conn.execute(
        "SELECT l.playlist_title AS genre, COUNT(*) AS n, "
        "       SUM(CASE WHEN tt.bpm IS NOT NULL THEN 1 ELSE 0 END) AS got "
        "FROM library_track l JOIN track_tempo tt USING (video_id) "
        "WHERE l.playlist_title LIKE 'C - %' GROUP BY l.playlist_title ORDER BY n DESC"
    ).fetchall()
    if rows:
        print("\nby your genre playlists:")
        for r in rows:
            print(f"    {r['genre']:28s} {r['got']:4d}/{r['n']:<4d} ({r['got'] / max(r['n'], 1) * 100:4.0f}%)")


if __name__ == "__main__":
    raise SystemExit(main())
