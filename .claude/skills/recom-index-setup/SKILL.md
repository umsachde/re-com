---
name: recom-index-setup
description: Build or refresh re-com's mood/genre/tempo/graph indexes in the correct order (build_atlas, label_library, build_genres, build_tempo, build_graph_atlas). Use for first-time setup of a new re-com install, or when re-crawling/refreshing indexes after a large library change.
---

These are offline maintenance scripts (`README.md` "Setup", `PLAN.md` §7.4) —
they authenticate directly with `ytmusicapi`/`headers_auth.json`, not through
`ytmusic-mcp`, and are run by hand from the command line. They are not part of
the live tool-call path. Full first-time setup takes roughly 45 minutes; most
of that is the atlas crawl in step 1.

## Order matters — run in this sequence

```bash
# 1. Crawl YouTube's editorial mood atlas (~35 min, resumable, safe to interrupt)
python scripts/build_atlas.py

# 2. Label your library from that atlas + artist propagation (needs only YouTube Music creds)
python scripts/label_library.py

# 3. Genre/language labels, for the language filter (~10-15 min)
python scripts/build_genres.py

# 4. Tempo/BPM, from Deezer's public API (~0.4s per song)
python scripts/build_tempo.py

# 5. Optional: cross-backend mood corpus via Deezer playlist search (works on any backend)
python scripts/build_graph_atlas.py

# 6. Optional: have Claude read lyrics to cover what the atlas missed (needs an API key)
pip install -e ".[llm]"
python scripts/label_library.py --claude
```

Step 5 (`build_graph_atlas.py`) is what makes mood tools work on Spotify at
all — the editorial atlas in step 1 is YouTube-only. If the user only cares
about YouTube Music, step 5 can be skipped, but skip it deliberately, not by
forgetting it.

## Checking progress without re-running anything

```bash
python scripts/build_atlas.py --status
python scripts/label_library.py --report
```

Or call the live `index_status()` MCP tool, which reports gaps rather than
just a total — useful for telling the user *what's missing*, not just *how
much exists*.

## Coverage expectations — don't be alarmed by partial numbers

These are measured, not bugs:

- Editorial atlas alone covers only ~4.1% of a typical liked library
  (English-centric mood playlists barely touch Punjabi/Bollywood/Reggae
  catalogues) — artist propagation (step 2) and the graph atlas (step 5) are
  what close most of that gap, not a bigger crawl of the same atlas.
- BPM coverage is uneven by genre (measured: Rock 67%, Hip-Hop 47%, down to
  Punjabi 6%) — Deezer genuinely doesn't have tempo for some tracks
  (`bpm: 0`), and re-com correctly leaves those unscored rather than dropping
  them.
- A full crawl typically lands around 70% total mood coverage on YouTube,
  lower on Spotify (~40%) since it relies entirely on the graph atlas rather
  than an editorial one.

If a coverage number comes back much lower than these, suspect an incomplete
or interrupted crawl (check `--status`/`--report`) before suspecting a code
regression.

## Keeping history real (optional, separate from the above)

`get_history()` only reports "Today"/"Yesterday", so timestamps need a cron
job, not a one-time script:

```
0 */3 * * * cd /path/to/re-com && .venv/bin/python scripts/snapshot_history.py
```

This feeds the implicit-feedback system (played/ignored inference) — it needs
no manual `record_feedback` calls to work, but it does need this cron running
continuously, unlike steps 1-6 above which are one-shot.
