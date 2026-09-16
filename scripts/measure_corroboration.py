#!/usr/bin/env python3
"""Measure how often _collapse_variants throws away cross-family corroboration.

PLAN.md 7.16. Native candidates are keyed by provider id, graph candidates by
`graph:<deezer id>` (`signals._add_graph_candidates`), so `_merge_and_score`
never merges a song that (say) YouTube radio and last.fm both named -- they
sit in the pool as two separate candidates, each at score 1. The only place
they meet is `_collapse_variants`, which recognises them as the same song by
title/artist but keeps the higher-scoring copy and drops the other's sources
rather than unioning them. Agreement between the native and graph families is
therefore structurally uncountable today.

This is read-only measurement, not a proposal. For each SIMILARITY_SEEDS
case it:

  1. gathers that seed's candidate pool once (same call quality_check.py
     makes for its A/B, so the two are comparable),
  2. clusters it the way `_collapse_variants` does, and counts clusters that
     mix a native-keyed candidate with a graph-keyed one,
  3. builds a counterfactual pool where those mixed clusters carry unioned
     `sources` and summed `score` before collapse/rank/resolve run exactly as
     they do today, and
  4. reports the corroborated share (score >= 2) both ways, split by
     catalogue (western / South Asian), because Spotify has no native
     signals to fragment against and should show ~zero effect -- a
     prediction worth checking, not assuming.

Nothing in signals.py is touched.

    python scripts/measure_corroboration.py
    python scripts/measure_corroboration.py --json out.json
"""

import argparse
import copy
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import signals  # noqa: E402
from scripts.quality_check import (  # noqa: E402
    GRAPH_SEEDS_SOUTH_ASIAN,
    GRAPH_SEEDS_WESTERN,
    _agreement,
    _source_ceiling,
)


def _cluster_pool(pool: dict) -> list[list[str]]:
    """Same clustering signals._collapse_variants does, without collapsing --
    every cluster of the same underlying song is returned, however many
    members it has, so this can tell native+graph mixes apart from the rest."""
    buckets: dict[str, list[str]] = {}
    for key, c in pool.items():
        buckets.setdefault(signals._song_key(c.get("title") or "") or key, []).append(key)

    clusters: list[list[str]] = []
    for keys in buckets.values():
        built: list[list[str]] = []
        for key in keys:
            c = pool[key]
            artist = (c.get("artists") or [None])[0]
            for cluster in built:
                rep = pool[cluster[0]]
                rep_artist = (rep.get("artists") or [None])[0]
                if signals.same_song(c.get("title"), artist, rep.get("title"), rep_artist):
                    cluster.append(key)
                    break
            else:
                built.append([key])
        clusters.extend(built)
    return clusters


def _is_native(pool: dict, key: str) -> bool:
    return not pool[key].get("graphRef")


def _is_mixed(pool: dict, cluster: list[str]) -> bool:
    return len(cluster) > 1 and any(_is_native(pool, k) for k in cluster) and any(
        not _is_native(pool, k) for k in cluster
    )


def _unioned_pool(pool: dict, clusters: list[list[str]]) -> dict:
    """Copy of pool where every mixed cluster's members all carry the
    cluster's unioned sources and summed score -- what collapse would see if
    it stopped dropping the loser's sources. Whichever member collapse then
    keeps carries that union forward."""
    out = copy.deepcopy(pool)
    for cluster in clusters:
        if not _is_mixed(pool, cluster):
            continue
        union_sources = set()
        total_score = 0
        for key in cluster:
            union_sources |= pool[key]["sources"]
            total_score += pool[key]["score"]
        for key in cluster:
            out[key]["sources"] = set(union_sources)
            out[key]["score"] = total_score
    return out


def _rank_and_resolve(yt, pool: dict, *, exclude, exclude_index, limit: int):
    ranked, _collapsed = signals._finalize(pool, exclude, limit, exclude_index=exclude_index)
    _pool_depth, searches = signals.resolve_budgets(limit)
    songs, _dropped = signals.resolve_candidates(yt, ranked, limit, exclude, max_resolve=searches)
    return songs


def measure(yt, graph_conn, *, limit: int = 10) -> dict:
    exclude = server._library_video_ids(yt)
    exclude_index = server._library_exclusion_index() if graph_conn else None
    ceiling = _source_ceiling(yt, graph_conn)
    pool_depth, _searches = signals.resolve_budgets(limit)

    rows = []
    for catalogue, seeds in (
        ("western", GRAPH_SEEDS_WESTERN),
        ("south_asian", GRAPH_SEEDS_SOUTH_ASIAN),
    ):
        for title, artist in seeds:
            vid = server._resolve_song_video_id(yt, title, artist)
            if not vid:
                rows.append({"catalogue": catalogue, "case": f"{title} — {artist}", "skipped": "seed unresolvable"})
                continue

            seed_meta = {vid: {"title": title, "artists": [artist]}}
            per_seed = signals.gather_seeds(
                yt, [vid], skip_failures=False, graph_conn=graph_conn, seed_meta=seed_meta
            )
            merged = signals._merge_and_score(per_seed)
            clusters = _cluster_pool(merged)
            mixed = [c for c in clusters if _is_mixed(merged, c)]

            baseline_songs = _rank_and_resolve(
                yt, merged, exclude=exclude, exclude_index=exclude_index, limit=limit
            )
            unioned_pool = _unioned_pool(merged, clusters) if mixed else merged
            unioned_songs = _rank_and_resolve(
                yt, unioned_pool, exclude=exclude, exclude_index=exclude_index, limit=limit
            )

            rows.append({
                "catalogue": catalogue,
                "case": f"{title} — {artist}",
                "pool_size": len(merged),
                "multi_member_clusters": sum(1 for c in clusters if len(c) > 1),
                "mixed_clusters": len(mixed),
                "mixed_cluster_sizes": sorted((len(c) for c in mixed), reverse=True),
                "corroborated_baseline": _agreement(baseline_songs, ceiling)["corroborated"],
                "corroborated_unioned": _agreement(unioned_songs, ceiling)["corroborated"],
            })

    scored = [r for r in rows if not r.get("skipped")]

    def _mean(field: str, subset=None) -> float | None:
        pool = subset if subset is not None else scored
        vals = [r[field] for r in pool if r.get(field) is not None]
        return round(statistics.mean(vals), 3) if vals else None

    by_catalogue = {}
    for catalogue in ("western", "south_asian"):
        subset = [r for r in scored if r["catalogue"] == catalogue]
        by_catalogue[catalogue] = {
            "n": len(subset),
            "total_mixed_clusters": sum(r["mixed_clusters"] for r in subset),
            "mean_corroborated_baseline": _mean("corroborated_baseline", subset),
            "mean_corroborated_unioned": _mean("corroborated_unioned", subset),
        }

    return {
        "provider": server.PROVIDER,
        "ceiling": ceiling,
        "total_mixed_clusters": sum(r["mixed_clusters"] for r in scored),
        "total_multi_member_clusters": sum(r["multi_member_clusters"] for r in scored),
        "mean_corroborated_baseline": _mean("corroborated_baseline"),
        "mean_corroborated_unioned": _mean("corroborated_unioned"),
        "by_catalogue": by_catalogue,
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", type=Path, default=None, help="also write full results here")
    parser.add_argument("--limit", type=int, default=10, help="songs per case")
    parser.add_argument("--no-graph", action="store_true", help="measure without the music graph (expect zero mixed clusters)")
    args = parser.parse_args()

    graph_conn = None
    if not args.no_graph:
        import graph_store

        graph_conn = graph_store.connect()

    yt = server._client()
    result = measure(yt, graph_conn, limit=args.limit)

    print(f"=== corroboration audit ({result['provider']}) ===")
    print(f"mixed clusters (native+graph, same song, different keys): {result['total_mixed_clusters']}"
          f" / {result['total_multi_member_clusters']} multi-member clusters")
    print(f"corroborated share: baseline {result['mean_corroborated_baseline']}"
          f"  ->  unioned {result['mean_corroborated_unioned']}")
    for catalogue, row in result["by_catalogue"].items():
        print(f"  {catalogue:<12} mixed_clusters={row['total_mixed_clusters']:<3} "
              f"corroborated {row['mean_corroborated_baseline']} -> {row['mean_corroborated_unioned']}  (n={row['n']})")
    print()
    for row in result["cases"]:
        if row.get("skipped"):
            print(f"  {row['case']:<32} skipped: {row['skipped']}")
            continue
        print(f"  {row['case']:<32} pool={row['pool_size']:<4} mixed={row['mixed_clusters']:<2} "
              f"corrob {row['corroborated_baseline']} -> {row['corroborated_unioned']}")

    if args.json:
        args.json.write_text(json.dumps(result, indent=2))
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
