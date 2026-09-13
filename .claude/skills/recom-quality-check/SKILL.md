---
name: recom-quality-check
description: Run re-com's scripts/quality_check.py and correctly interpret its metrics (mood fit, cross-mood overlap, similarity signal agreement, noise floor). Use when judging whether a ranking, signal, or scoring change to re-com actually helped or hurt.
---

`scripts/quality_check.py` is the third verification layer (`PLAN.md` §5) —
it turns ranking-quality questions into numbers instead of impressions. Run it
by hand whenever a change touches ranking, signal weighting, mood scoring, or
the graph.

## Commands

```bash
python scripts/quality_check.py --titles                    # mood/arc cases, human-readable output
python scripts/quality_check.py --distinctiveness 0          # A/B the seed-scoring logic
python scripts/quality_check.py --similarity --repeat        # scores recommend_from_song / recommend_from_playlist, plus a noise floor in the same run
```

Always prefer `--repeat` when comparing two versions of the ranking logic — it
measures the noise floor (run-to-run variance) in the same invocation, so a
delta can be judged against it instead of against a stale number from
`PLAN.md`.

## The metrics, and what they actually mean

- **Mean mood fit** — average fit score. **Do not stop here.** `PLAN.md` §3
  records a build that scored a healthy 0.775 mean fit while returning 70% the
  same songs for "heartbroken" and "angry" — fit alone cannot see that kind of
  collapse.
- **Cross-mood overlap** (lower is better) — the metric that *does* catch that
  failure mode. Always check this alongside mean fit, never instead of it.
- **Cross-seed overlap** (lower is better, `--similarity` only) — the
  same-shape check for the similarity path: whether many different seeds
  funnel into one popular attractor.
- **Signal agreement / corroborated** (`--similarity`) — reported **against a
  per-backend ceiling**, not as a raw count. YouTube's ceiling is higher than
  Spotify's because Spotify has fewer native signals (`capabilities()` is
  emptier there) — a bare cross-backend mean would misread that difference as
  a regression.
- **Native-vs-graph A/B: churn vs. corroboration delta** — churn alone cannot
  answer "did this help or dilute", because both arms truncate to the same
  `limit` and can show identical churn/additions by pure arithmetic
  (`PLAN.md` §6.5 — this happened, 37 == 37). The **corroboration delta** is
  the number that actually answers helping-vs-diluting.
- **Artist concentration (HHI)** — measure it *before* any `max_per_artist`
  cap is applied; measuring after only confirms the cap works, it doesn't
  tell you anything about the underlying ranking.

## The noise floor is backend-specific — never assume one number

`PLAN.md` §3/§7.2: YouTube's noise floor (identical runs, `--repeat`) measures
**~0.87** because its radio/related endpoints vary run to run. Spotify's
measures **1.00** — bit-identical results, because every candidate comes from
the locally cached graph and nothing upstream varies. Consequences:

- A 5% delta on YouTube is noise. The same 5% delta on Spotify is real.
- Always state which backend a reported delta was measured on — a number
  without that label is not usable for comparison later.

## Sanity-check the harness itself, not just the numbers it reports

`PLAN.md` §7.11 records the harness silently carrying its own copy of pool-
sizing logic, so it kept reporting a stale figure after the real server was
fixed — a harness that duplicates the logic it measures can end up measuring
something that no longer ships. If a `quality_check.py` number looks stuck
after a code change that should have moved it, check whether the script
re-derives any logic that now lives elsewhere in the codebase instead of
calling it.
