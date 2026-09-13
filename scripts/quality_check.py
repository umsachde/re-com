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

`--similarity` measures the OTHER half of the engine: recommend_from_song and
recommend_from_playlist, which had no number at all until now and were judged
by impression. It reports signal-agreement distribution, artist concentration,
cross-seed overlap and a native-vs-graph A/B. See measure_similarity.

Like scripts/smoke_all.py this needs live credentials and a real account, so it
cannot gate CI -- it is a by-hand tool, the third layer in PLAN.md 5.

    python scripts/quality_check.py                 # measure current behaviour
    python scripts/quality_check.py --label run-name --json out.json
    python scripts/quality_check.py --distinctiveness 0   # A/B the seed scoring
    python scripts/quality_check.py --similarity            # the similarity path
    python scripts/quality_check.py --similarity --repeat   # + a noise floor
"""

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter
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


# --- the similarity path ----------------------------------------------------
#
# PLAN.md 7.2: `recommend_from_song` and `recommend_from_playlist` are the
# most-used tools and the only ones with no number attached. 3's own lesson --
# a healthy 0.775 mean fit hid 70% cross-mood duplication -- says impression is
# not good enough.

SIMILARITY_SEEDS = GRAPH_SEEDS_WESTERN + GRAPH_SEEDS_SOUTH_ASIAN

# The sources graph.neighbours tags its rows with. A candidate's score counts
# distinct (seed, source) pairs, so the ceiling depends on the backend AND on
# the seed count: 9 per seed on YouTube, 6 on Spotify, whose capabilities()
# returns the empty set by measurement rather than pessimism. A raw mean read
# without that ceiling would report arithmetic as a regression.
#
# `graph_related_lb` is PLAN.md 7.3's second source and MUST be counted here,
# for the sake of the one number that justified adding it. The baseline said
# 87% of Spotify's picks rested on a single signal; if the ceiling stayed at 3
# while a fourth source started contributing, that number would improve partly
# by arithmetic and the measurement would be flattering itself.
GRAPH_SOURCES = ("graph_artist", "graph_radio", "graph_related", "graph_related_lb", "graph_similar_lb", "graph_similar_lfm")


def _source_ceiling(yt, graph_conn) -> int:
    import provider as provider_module

    caps = provider_module.capabilities_of(yt)
    return len(caps) + (len(GRAPH_SOURCES) if graph_conn is not None else 0)


def _run_similarity(yt, seed_ids, seed_meta, *, exclude, exclude_index, graph_conn, limit):
    """The shipping similarity pipeline, over a PINNED seed list.

    Mirrors server.recommend_from_song / recommend_from_playlist minus three
    things, each deliberate. The MCP decorator and the language/tempo filters
    answer a different question. The third is the one that matters:
    recommend_from_playlist picks its seeds with random.sample, which is right
    for the tool and fatal for a measurement -- two runs that do not share
    seeds cannot be A/B'd, because the delta would be sampling noise.

    No per-artist cap either. _apply_result_filters' max_per_artist would hold
    concentration at 2/limit by construction, so measuring after it confirms
    the cap works instead of measuring what the ranking actually produces.
    """
    import signals

    per_seed = signals.gather_seeds(
        yt, seed_ids, skip_failures=False, graph_conn=graph_conn, seed_meta=seed_meta
    )
    merged = signals._merge_and_score(per_seed)
    # From signals, never re-derived here: this harness paired these two numbers
    # by hand and paired them differently, so after 7.11 deepened the server's
    # pool it went on reporting a short result the real tool no longer returned.
    pool, searches = signals.resolve_budgets(limit)
    ranked, _collapsed = signals._finalize(merged, exclude, pool, exclude_index=exclude_index)
    songs, _unresolved = signals.resolve_candidates(
        yt, ranked, limit, exclude, max_resolve=searches
    )
    return songs


def _titles(songs) -> set[str]:
    return {f"{s['title']} — {', '.join(s.get('artists') or [])}" for s in songs}


def _overlap_pairs(sets: dict[str, set]) -> list:
    """Pairwise overlap, worst first. Shared by both paths so cross-mood and
    cross-seed overlap mean the same thing."""
    names = [n for n in sets if sets[n]]
    return sorted(
        (
            (round(len(sets[a] & sets[b]) / max(len(sets[a]), 1), 2), a, b)
            for i, a in enumerate(names)
            for b in names[i + 1:]
        ),
        reverse=True,
    )


def _agreement(songs, ceiling: int) -> dict:
    """Signal agreement, always reported against its ceiling.

    `corroborated` is the headline: the share of picks that more than one
    independent (seed, source) pair surfaced. A pool where nearly everything
    scores 1 is a pool with no agreement to rank on -- the engine's whole
    premise (4.1) degrading quietly to "whatever one signal said".
    """
    scores = [s.get("score", 0) for s in songs]
    if not scores:
        return {"n": 0, "ceiling": ceiling, "mean": None, "corroborated": None, "histogram": {}}
    mean = statistics.mean(scores)
    return {
        "n": len(scores),
        "ceiling": ceiling,
        "mean": round(mean, 2),
        "normalized": round(mean / ceiling, 3) if ceiling else None,
        "corroborated": round(sum(1 for s in scores if s >= 2) / len(scores), 3),
        "histogram": dict(sorted(Counter(scores).items())),
    }


def _concentration(songs) -> dict:
    """How much of a result set one artist owns. HHI (sum of squared shares)
    reads the whole distribution; max_artist_share catches the single hog."""
    primaries = [((s.get("artists") or ["?"])[0] or "?").lower() for s in songs]
    if not primaries:
        return {"distinct_artists": 0, "hhi": None, "max_artist_share": None}
    counts = Counter(primaries)
    n = len(primaries)
    return {
        "distinct_artists": len(counts),
        "hhi": round(sum((c / n) ** 2 for c in counts.values()), 3),
        "max_artist_share": round(max(counts.values()) / n, 3),
    }


def _graph_only_share(songs) -> float | None:
    """Share of picks no native signal had -- what the graph actually added."""
    if not songs:
        return None
    graph_only = sum(
        1 for s in songs
        if s.get("sources") and all(src in GRAPH_SOURCES for src in s["sources"])
    )
    return round(graph_only / len(songs), 3)


def _ab(with_graph, native, graph_titles, native_titles, native_ceiling) -> dict:
    """Native-vs-graph, the honest version.

    The first live run killed the metric this started as. "How many native
    picks did the graph displace" sounds like it answers whether the graph
    helps, and does not: both arms are truncated to `limit`, so whenever both
    fill up, displaced == added identically -- it was 37 == 37 across ten
    YouTube cases. That is arithmetic wearing a finding's clothes.

    What it actually measures is churn, so it is reported once under that name.
    The question churn cannot answer -- is what came in better than what went
    out -- needs the corroboration delta: how much agreement backs the picks
    with the graph on versus off. Positive means the graph is adding songs more
    signals concur on; negative means it is diluting.

    Read the delta with one caveat: the graph arm has more sources available to
    agree with each other, so some positive bias is structural. What that bias
    cannot manufacture is a negative sign, or a collapse on one catalogue while
    another improves -- which is the shape worth acting on.
    """
    native_agreement = _agreement(native, native_ceiling)
    graph_corroborated = _agreement(with_graph, native_ceiling)["corroborated"]
    delta = None
    if graph_corroborated is not None and native_agreement["corroborated"] is not None:
        delta = round(graph_corroborated - native_agreement["corroborated"], 3)
    return {
        "native_n": len(native),
        "native_agreement": native_agreement,
        "churn": len(graph_titles - native_titles),
        "corroboration_delta": delta,
    }


def _pinned_playlist_seeds(yt, count: int = 5):
    """(playlist_id, tracks) for a real library playlist, or None.

    Same selection rule as smoke_all._multi_track_playlist -- 2+ readable
    tracks, verified by reading rather than trusting `count`, because Spotify
    reports none and trusting it once picked an empty playlist. Then the first
    `count` tracks in playlist order, pinned so the A/B is comparable.
    """
    try:
        playlists = yt.get_library_playlists(limit=25)
    except Exception:  # noqa: BLE001 - no playlist is a skip, reported by the caller
        return None

    for pl in playlists or []:
        playlist_id = pl.get("playlistId")
        if not playlist_id:
            continue
        try:
            tracks = (yt.get_playlist(playlist_id, limit=max(count, 5)) or {}).get("tracks") or []
        except Exception:  # noqa: BLE001 - unreadable is simply not a candidate
            continue
        usable = [t for t in tracks if t.get("videoId")][:count]
        if len(usable) >= 2:
            return playlist_id, usable
    return None


def _similarity_cases(yt, with_playlist: bool):
    """(name, seed_ids, seed_meta) per case. Seeds reuse the graph seed lists,
    already split by catalogue, so the graph-coverage numbers and these
    describe the same songs.

    The playlist case is not optional padding: 6.1's defect was invisible on
    the single-seed path (it stays on the calling thread) and fatal on the
    multi-seed one, and multi-seed agreement -- the (seed, source) pair count
    the engine actually ranks on -- only exists here.
    """
    import signals

    cases = []
    for title, artist in SIMILARITY_SEEDS:
        vid = server._resolve_song_video_id(yt, title, artist)
        if not vid:
            cases.append((f"{title} — {artist}", None, None))
            continue
        cases.append((f"{title} — {artist}", [vid], {vid: {"title": title, "artists": [artist]}}))

    if with_playlist:
        picked = _pinned_playlist_seeds(yt)
        if picked is None:
            cases.append(("playlist (pinned seeds)", None, None))
        else:
            playlist_id, tracks = picked
            ids = [t["videoId"] for t in tracks]
            cases.append((
                f"playlist {playlist_id} ({len(ids)} pinned seeds)",
                ids,
                {t["videoId"]: signals._norm_track(t) for t in tracks},
            ))
    return cases


def measure_similarity(yt, *, graph_conn=None, limit: int = 10,
                       repeat: bool = False, with_playlist: bool = True) -> dict:
    """Four numbers for the similarity path, plus a noise floor.

    agreement      how many independent (seed, source) pairs backed each pick,
                   against the ceiling this backend and seed count allow
    concentration  how much of a result set one artist owns, uncapped
    cross-seed     how much UNRELATED seeds return the same songs. The direct
                   analogue of cross-mood overlap, and for the same reason the
                   one to watch: high overlap means the engine funnels every
                   seed into the same popular attractor.
    native vs graph  churn (how much of the top `limit` the graph replaced) and
                   the corroboration delta (whether what replaced it is better
                   agreed-upon). The delta is the one that answers "helping or
                   diluting" -- see _ab.

    `repeat` re-runs each case's graph arm and reports self-overlap. 3 measured
    two identical serial runs overlapping 0.793, so an A/B delta under roughly
    20% is indistinguishable from API variance. A metric that does not say so
    invites exactly 6.2's mistake -- reading a number without knowing what it
    protects.
    """
    exclude = server._library_video_ids(yt)
    exclude_index = server._library_exclusion_index() if graph_conn else None
    per_seed_ceiling = _source_ceiling(yt, graph_conn)
    native_ceiling = _source_ceiling(yt, None)

    rows = []
    for name, seed_ids, seed_meta in _similarity_cases(yt, with_playlist):
        if not seed_ids:
            rows.append({"case": name, "skipped": "no seed resolvable on this backend"})
            continue

        started = time.time()
        with_graph = _run_similarity(
            yt, seed_ids, seed_meta, exclude=exclude, exclude_index=exclude_index,
            graph_conn=graph_conn, limit=limit,
        )
        seconds = round(time.time() - started, 1)

        # exclude_index is None here on purpose: it only exists to catch graph
        # candidates, which have no provider id. The native arm has none.
        native = _run_similarity(
            yt, seed_ids, seed_meta, exclude=exclude, exclude_index=None,
            graph_conn=None, limit=limit,
        )

        graph_titles, native_titles = _titles(with_graph), _titles(native)
        row = {
            "case": name,
            "seeds": len(seed_ids),
            "n": len(with_graph),
            "seconds": seconds,
            "agreement": _agreement(with_graph, per_seed_ceiling * len(seed_ids)),
            "concentration": _concentration(with_graph),
            "graph_only_share": _graph_only_share(with_graph),
            "ab": _ab(with_graph, native, graph_titles, native_titles,
                      native_ceiling * len(seed_ids)),
            "titles": sorted(graph_titles),
        }
        if repeat:
            again = _run_similarity(
                yt, seed_ids, seed_meta, exclude=exclude, exclude_index=exclude_index,
                graph_conn=graph_conn, limit=limit,
            )
            row["self_overlap"] = round(
                len(graph_titles & _titles(again)) / max(len(graph_titles), 1), 2
            )
        rows.append(row)

    scored = [r for r in rows if not r.get("skipped")]
    sets = {r["case"]: set(r["titles"]) for r in scored}
    pairs = _overlap_pairs(sets)
    corroborated = [
        r["agreement"]["corroborated"] for r in scored if r["agreement"]["corroborated"] is not None
    ]
    floors = [r["self_overlap"] for r in scored if r.get("self_overlap") is not None]
    deltas = [
        r["ab"]["corroboration_delta"] for r in scored
        if r["ab"]["corroboration_delta"] is not None
    ]

    return {
        "provider": server.PROVIDER,
        "per_seed_ceiling": per_seed_ceiling,
        "native_ceiling": native_ceiling,
        "mean_corroborated": round(statistics.mean(corroborated), 3) if corroborated else None,
        "mean_hhi": round(
            statistics.mean(r["concentration"]["hhi"] for r in scored
                            if r["concentration"]["hhi"] is not None), 3
        ) if scored else None,
        "cross_seed_overlap": round(statistics.mean(p[0] for p in pairs), 3) if pairs else None,
        "worst_overlaps": pairs[:4],
        "distinct_songs": len(set().union(*sets.values())) if sets else 0,
        "total_slots": sum(len(v) for v in sets.values()),
        "graph_churn": sum(r["ab"]["churn"] for r in scored),
        "mean_corroboration_delta": round(statistics.mean(deltas), 3) if deltas else None,
        "noise_floor": round(statistics.mean(floors), 3) if floors else None,
        "cases": rows,
    }


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
    pairs = _overlap_pairs(sets)
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
    parser.add_argument("--similarity", action="store_true",
                        help="measure the similarity path (recommend_from_song / _from_playlist) instead of mood")
    parser.add_argument("--repeat", action="store_true",
                        help="with --similarity, re-run each case to establish a noise floor for the A/B")
    parser.add_argument("--no-playlist", action="store_true",
                        help="with --similarity, skip the multi-seed playlist case")
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

    if args.similarity:
        # No mood index needed: the similarity path never reads a label, so
        # this runs on a fresh install where the mood measurement cannot.
        graph_conn = None
        if not args.no_graph:
            import graph_store

            graph_conn = graph_store.connect()
        result = measure_similarity(
            server._client(), graph_conn=graph_conn, limit=args.limit,
            repeat=args.repeat, with_playlist=not args.no_playlist,
        )
        result["label"] = args.label

        print(f"=== {args.label}: similarity path ({result['provider']}) ===")
        print(f"ceiling {result['per_seed_ceiling']}/seed with graph, "
              f"{result['native_ceiling']}/seed native")
        print(f"corroborated {result['mean_corroborated']} (share of picks >1 signal agreed on) "
              f"| concentration HHI {result['mean_hhi']} (lower is better)")
        print(f"cross-seed overlap {result['cross_seed_overlap']} (lower is better) "
              f"| {result['distinct_songs']} distinct across {result['total_slots']} slots")
        mean_delta = result["mean_corroboration_delta"]
        print(f"graph churn {result['graph_churn']} of {result['total_slots']} slots "
              f"| corroboration delta {'n/a' if mean_delta is None else format(mean_delta, '+')} "
              f"(graph on vs off; >0 means the graph is adding agreement, not diluting)")
        if result["noise_floor"] is not None:
            print(f"noise floor: identical runs overlap {result['noise_floor']} "
                  f"-- read every delta above against this")
        else:
            print("noise floor: not measured (pass --repeat); PLAN.md 3 measured 0.793")
        for share, a, b in result["worst_overlaps"]:
            print(f"    overlap {share:.0%}: {a}  vs  {b}")
        for row in result["cases"]:
            if row.get("skipped"):
                print(f"  {row['case']:38s} SKIPPED -- {row['skipped']}")
                continue
            agree, conc = row["agreement"], row["concentration"]
            print(f"  {row['case']:38s} n={row['n']:<3} corrob {agree['corroborated']} "
                  f"(mean {agree['mean']}/{agree['ceiling']}) artists {conc['distinct_artists']} "
                  f"hhi {conc['hhi']} graph-only {row['graph_only_share']} {row['seconds']}s")
            delta = row["ab"]["corroboration_delta"]
            print(f"       {'':36s} vs native: n={row['ab']['native_n']} "
                  f"churn {row['ab']['churn']} corrob delta "
                  f"{'n/a' if delta is None else format(delta, '+')}"
                  + (f" | self-overlap {row['self_overlap']}" if "self_overlap" in row else ""))
            if args.titles:
                for title in row["titles"]:
                    print(f"       {title}")

        if args.json:
            args.json.write_text(json.dumps(result, indent=1))
            print(f"\nwrote {args.json}")
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
