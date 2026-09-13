---
name: recom-smoke-test
description: Run re-com's live smoke test (scripts/smoke_all.py) across backends before a release, and interpret pass/fail/skipped correctly. Use before merging/releasing re-com changes, or when asked to verify re-com works against the real accounts.
---

`scripts/smoke_all.py` is the live verification layer (`PLAN.md` §5) — it runs
every tool against every *configured* backend, against the real account, and
is the only layer that can actually check the library-exclusion guarantee and
real latency. Unit tests (`pytest`) cannot substitute for this; they run
against fakes and structurally cannot see a real-connection or real-account
defect (see `PLAN.md` §6.1 and §6.6 for two that unit tests missed and this
harness caught).

## How to run it

```bash
python scripts/smoke_all.py                      # every configured backend
python scripts/smoke_all.py --provider spotify    # just one backend
python scripts/smoke_all.py --include-writes      # also exercises record_feedback (writes to the real store)
```

Each backend runs in its own subprocess, because `RECOM_PROVIDER` is read once
at import — one process cannot honestly test two backends.

## How to read the result — do not treat `skipped` as a pass

Three invariants are checked per tool, per backend:

- **returns** — a non-empty result, or an explicit stated reason (e.g. "no
  mood index for this backend"). Silent emptiness is the failure being hunted
  for.
- **excludes** — nothing already in the library. This is the project's core
  guarantee (`PLAN.md` §1, "The guarantee") and a live run is the only place
  it can actually be tested.
- **within** — a latency ceiling. A tool quietly taking 60s has regressed even
  if the songs returned are correct.

A backend with no `*-mcp` command configured is reported as `skipped`, **never**
as a pass — don't read a skip as "that backend is fine," it means it wasn't
tested at all. `record_feedback` only runs under `--include-writes` because it
writes to the real store; a run without that flag correctly skips it.

## Baseline to compare against

The last recorded baseline (`PLAN.md` §7.1, 2026-09-10, warm, `limit=10`,
graph on) had every tool passing on both backends, with the mood/playlist-mood
path notably slower on YouTube (~27s) than the single-seed path (~5-8s) — a
slow mood-path result on YouTube is not automatically a regression; compare
against this baseline rather than an assumed flat latency ceiling. If a run
disagrees sharply with these numbers, treat it as a signal to investigate, not
noise.

## Before merging a change that touches signals, ranking, or the provider seam

Run this against every backend you have credentials for, not just the one you
were changing — cross-backend defects (like the thread-safety bug in
`PLAN.md` §6.1) have shipped silently through gaps exactly this harness exists
to close.
