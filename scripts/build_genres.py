#!/usr/bin/env python3
"""Harvest genre labels from YouTube's genre-category pages.

Feeds the language filter (see taxonomy.py). Roughly 10-15 minutes for all 27
genres; safe to re-run, and each genre is independent so an interruption only
loses the genre in flight.

    python scripts/build_genres.py
    python scripts/build_genres.py --playlists 25   # deeper, slower
    python scripts/build_genres.py --status

**Deliberately still on ytmusicapi**, for the same reason as build_atlas.py:
it parses YouTube's genre-category browse responses, which exist nowhere else.
Not an oversight in v6's migration of the other offline scripts -- do not
"fix" it onto the Provider seam.
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402
import taxonomy  # noqa: E402
from ytmusicapi import YTMusic  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--playlists", type=int, default=12, help="playlists to crawl per genre")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    conn = store.connect()
    if args.status:
        _report(conn)
        return 0

    auth = os.environ.get("RECOM_AUTH_PATH", "headers_auth.json")
    if not Path(auth).exists():
        print(f"error: {auth} not found. Run scripts/setup_auth_from_file.py first.", file=sys.stderr)
        return 1

    started = time.time()
    stats = taxonomy.crawl_genres(
        YTMusic(auth), conn, playlists_per_genre=args.playlists,
        on_progress=lambda p: print(f"  {p['genre']:28s} {p['songs']:7,d} labels", flush=True),
    )
    print(f"\n{stats['genres']} genres, {stats['playlists']} playlists, "
          f"{stats['songs']:,} labels in {(time.time() - started) / 60:.1f}m")
    _report(conn)
    return 0


def _report(conn) -> None:
    stats = store.genre_stats(conn)
    print(f"\ngenre-labelled tracks: {stats['tracks']:,}")
    for genre, n in list(stats["genres"].items())[:10]:
        print(f"    {genre:28s} {n:7,d}")
    languages = taxonomy.artist_languages(conn)
    from collections import Counter
    print(f"\nartists with a language: {len(languages):,}")
    for lang, n in Counter(languages.values()).most_common():
        print(f"    {lang:12s} {n:6,d} artists")


if __name__ == "__main__":
    raise SystemExit(main())
