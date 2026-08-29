#!/usr/bin/env python3
"""Measure recommendation quality, so "better" is a number rather than a hunch.

Runs a fixed set of mood/arc cases and reports:

  mean fit           how well picks match the mood they were asked for
  cross-mood overlap how much different moods return the SAME songs
  distinct songs     total unique songs across every case
  rated              what fraction of picks carry a real mood label
  artists/10         variety within a single result

Cross-mood overlap is the important one and the reason this script exists. A
run once scored a healthy 0.775 mean fit while "heartbroken" and "angry"
returned 70% the same songs -- the engine had only 44 distinct songs to offer
across 8 moods. Fit alone cannot see that; overlap can.

    python scripts/quality_check.py                 # measure current behaviour
    python scripts/quality_check.py --label run-name --json out.json
    python scripts/quality_check.py --distinctiveness 0   # A/B the seed scoring
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import label as label_mod  # noqa: E402
import moodspace  # noqa: E402
import recommend  # noqa: E402
import store  # noqa: E402
import server  # noqa: E402

CASES = [
    ("heartbroken and low", None, "mirror"),
    ("heartbroken and low", None, "lift"),
    ("need to focus and get work done", None, "mirror"),
    (None, "Workout", "hold"),
    ("chill evening winding down", None, "settle"),
    ("angry", None, "mirror"),
    ("nostalgic", None, "mirror"),
    (None, "Party", "mirror"),
]


# Seeds for the v6 graph measurement, deliberately split by catalogue. The
# Punjabi/Bollywood half is the part every English-centric source under-serves,
# and the part PLAN.md warns must never be read off the BPM coverage number --
# those tracks resolve to the right Deezer record and simply carry `bpm: 0`, so
# 36% tempo coverage says nothing about graph coverage.
GRAPH_SEEDS_WESTERN = [
    ("Blinding Lights", "The Weeknd"),
    ("Kryptonite", "3 Doors Down"),
    ("As It Was", "Harry Styles"),
    ("Bad Guy", "Billie Eilish"),
]
GRAPH_SEEDS_SOUTH_ASIAN = [
    ("Excuses", "AP Dhillon"),
    ("Brown Munde", "AP Dhillon"),
    ("295", "Sidhu Moose Wala"),
    ("Channa Mereya", "Arijit Singh"),
    ("Kesariya", "Arijit Singh"),
]


def measure_graph(graph_conn, limit: int = 10) -> dict:
    """v6: does the neutral graph actually cover this library's catalogue?

    Reports resolution and adjacency separately per catalogue, because the
    whole premise of choosing Deezer was that it covers the Punjabi/Bollywood
    material well even though it has little *tempo* data for it.
    """
    import graph

    out = {}
    for name, seeds in (
        ("western", GRAPH_SEEDS_WESTERN),
        ("south_asian", GRAPH_SEEDS_SOUTH_ASIAN),
    ):
        resolved = related = radio = neighbours = 0
        for title, artist in seeds:
            seed = graph.resolve(graph_conn, title, artist)
            if not seed:
                continue
            resolved += 1
            if graph.related_artists(graph_conn, seed["artist_id"]):
                related += 1
            if graph.artist_tracks(graph_conn, seed["artist_id"], graph.KIND_RADIO):
                radio += 1
            neighbours += len(graph.neighbours(graph_conn, seed, per_artist=limit))
        n = len(seeds)
        out[name] = {
            "seeds": n,
            "resolved": resolved,
            "resolved_pct": round(100 * resolved / n, 1),
            "with_related_artists": related,
            # PLAN.md recorded /artist/{id}/radio as empty for AP Dhillon; a
            # 2026-08-24 re-probe returned 25 tracks for that same artist.
            # This column is what settles it.
            "with_artist_radio": radio,
            "mean_neighbours": round(neighbours / max(resolved, 1), 1),
        }
    return out


def measure(yt, conn, limit: int = 10, graph_conn=None) -> dict:
    """`graph_conn` mirrors what the server passes, so this measures the
    pipeline that ships rather than a graph-blind variant of it."""
    exclude = store.library_video_ids(conn) | store.rejected_video_ids(conn)
    exclude_index = server._library_exclusion_index() if graph_conn else None
    rows = []
    for feeling, context, arc in CASES:
        started = time.time()
        result = recommend.build(yt, conn, exclude=exclude, feeling=feeling,
                                 context=context, arc=arc, limit=limit,
                                 graph_conn=graph_conn, exclude_index=exclude_index)
        songs = result["songs"]
        fits = [s["mood_fit"] for s in songs if s["mood_fit"] is not None]
        rows.append({
            "case": f"{feeling or context}/{arc}",
            "n": len(songs),
            "rated": sum(1 for s in songs if s["rated"]),
            "mean_fit": round(statistics.mean(fits), 3) if fits else None,
            "min_fit": round(min(fits), 3) if fits else None,
            "artists": len({(s["artists"] or ["?"])[0] for s in songs}),
            "seconds": round(time.time() - started, 1),
            "titles": [f"{s['title']} — {', '.join(s['artists'])}" for s in songs],
        })

    sets = {r["case"]: set(r["titles"]) for r in rows}
    names = [n for n in sets if sets[n]]
    pairs = sorted(
        ((round(len(sets[a] & sets[b]) / max(len(sets[a]), 1), 2), a, b) for i, a in enumerate(names) for b in names[i + 1:]),
        reverse=True,
    )
    fits = [r["mean_fit"] for r in rows if r["mean_fit"] is not None]

    return {
        "library_coverage": label_mod.library_coverage(conn),
        "atlas": {k: v for k, v in store.atlas_stats(conn).items() if k != "moods"},
        "mean_fit": round(statistics.mean(fits), 3) if fits else None,
        "cross_mood_overlap": round(statistics.mean(p[0] for p in pairs), 3) if pairs else None,
        "distinct_songs": len(set().union(*sets.values())) if sets else 0,
        "total_slots": sum(len(v) for v in sets.values()),
        "rated_fraction": round(sum(r["rated"] for r in rows) / max(sum(r["n"] for r in rows), 1), 3),
        "mean_artists": round(statistics.mean(r["artists"] for r in rows), 2),
        "worst_overlaps": pairs[:4],
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="run", help="name for this run")
    parser.add_argument("--json", type=Path, default=None, help="also write full results here")
    parser.add_argument("--limit", type=int, default=10, help="songs per case")
    parser.add_argument("--distinctiveness", type=float, default=None,
                        help="override moodspace.DISTINCTIVENESS_WEIGHT (0 disables it) to A/B the seed scoring")
    parser.add_argument("--titles", action="store_true", help="print every pick")
    parser.add_argument("--graph", action="store_true",
                        help="measure music-graph coverage only (needs no mood index, works on any backend)")
    parser.add_argument("--no-graph", action="store_true",
                        help="measure without the music graph, to A/B what it contributes")
    parser.add_argument("--languages", action="store_true",
                        help="split library mood coverage by catalogue language and exit")
    args = parser.parse_args()

    # No auth check here any more: the sibling *-mcp server owns credentials
    # and reports a clear, actionable error itself if they're missing.
    if args.distinctiveness is not None:
        moodspace.DISTINCTIVENESS_WEIGHT = args.distinctiveness

    if args.languages:
        conn = store.connect()
        rows = label_mod.library_coverage_by_language(conn)
        if not rows:
            print("error: no library recorded. Run scripts/label_library.py first.", file=sys.stderr)
            return 1
        print(f"=== mood coverage by catalogue ({server.PROVIDER}) ===")
        for language, row in rows.items():
            sources = " ".join(f"{k}={v}" for k, v in row["by_source"].items()) or "-"
            print(
                f"  {language:<10} {row['labelled']:>5}/{row['library']:<5} "
                f"({row['coverage'] * 100:5.1f}%)  {sources}"
            )
        return 0

    if args.graph:
        import graph_atlas
        import graph_store

        graph_conn = graph_store.connect()
        print(f"=== music graph ({server.PROVIDER}) ===")
        for catalogue, row in measure_graph(graph_conn, limit=args.limit).items():
            print(
                f"  {catalogue:<12} resolved {row['resolved']}/{row['seeds']} "
                f"({row['resolved_pct']}%)  related={row['with_related_artists']}  "
                f"radio={row['with_artist_radio']}  mean_neighbours={row['mean_neighbours']}"
            )
        print("\n  graph cache:")
        for key, value in graph_store.stats(graph_conn).items():
            print(f"    {key:<22} {value:,}")
        print("\n  graph atlas:")
        for key, value in graph_atlas.coverage(graph_conn).items():
            print(f"    {key:<22} {value:,}")
        return 0

    conn = store.connect()
    if not store.library_video_ids(conn):
        print("error: no library recorded. Run scripts/label_library.py first.", file=sys.stderr)
        return 1

    graph_conn = None
    if not args.no_graph:
        import graph_store
        graph_conn = graph_store.connect()
    result = measure(server._client(), conn, limit=args.limit, graph_conn=graph_conn)
    result["label"] = args.label

    cov = result["library_coverage"]
    print(f"=== {args.label} ===")
    print(f"atlas {result['atlas']['playlists_ok']:,} listings / {result['atlas']['unique_tracks']:,} tracks "
          f"| library coverage {cov['coverage'] * 100:.1f}% {cov['by_source']}")
    print(f"mean fit {result['mean_fit']} | cross-mood overlap {result['cross_mood_overlap']} (lower is better)")
    print(f"{result['distinct_songs']} distinct songs across {result['total_slots']} slots "
          f"| rated {result['rated_fraction'] * 100:.0f}% | artists/10 {result['mean_artists']}")
    for share, a, b in result["worst_overlaps"]:
        print(f"    overlap {share:.0%}: {a}  vs  {b}")
    for row in result["cases"]:
        print(f"  {row['case']:38s} fit {str(row['mean_fit']):5s} (min {row['min_fit']}) "
              f"rated {row['rated']}/{row['n']} artists {row['artists']} {row['seconds']}s")
        if args.titles:
            for title in row["titles"]:
                print(f"       {title}")

    if args.json:
        args.json.write_text(json.dumps(result, indent=1))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
