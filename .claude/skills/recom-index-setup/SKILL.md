---
name: recom-index-setup
description: Build or refresh re-com's mood/genre/tempo/graph indexes in the correct order (build_atlas, label_library, build_genres, build_tempo, build_graph_atlas). Use for first-time setup of a new re-com install, or when re-crawling/refreshing indexes after a large library change.
---

These are offline maintenance scripts (`README.md` "Setup", `PLAN.md` §7.4), run
by hand from the command line. They are not part of the live tool-call path.
Full first-time setup takes roughly 45 minutes; most of that is the atlas crawl
in step 1.

Use `.venv/bin/python`, not `python` — the deps are in the venv. (Its console
scripts such as `.venv/bin/pytest` may carry a stale shebang from an earlier
project name; `.venv/bin/python -m pytest` works regardless.)

## Credentials differ per script — check before running

v6 moved *some* of these onto the `provider.Provider` seam and left others on
`ytmusicapi` deliberately. The seam scripts reach the account through the
`ytmusic-mcp` subprocess, so they need it pointed out to them in the
environment, and **crash on startup without it**:

```
RuntimeError: ytmusic-mcp is not configured. Set RECOM_YTMUSIC_MCP_COMMAND ...
```

| script | how it reaches the account |
| --- | --- |
| `build_atlas.py` | `ytmusicapi` directly, `RECOM_AUTH_PATH` (default `headers_auth.json`) |
| `build_genres.py` | `ytmusicapi` directly, `RECOM_AUTH_PATH` |
| `build_tempo.py` | neither — store + Deezer's public API only |
| `build_graph_atlas.py` | neither — Deezer + the store (propagate is store-side) |
| `label_library.py` | **Provider seam → needs the env below** |
| `snapshot_history.py` | **Provider seam → needs the env below** |
| `quality_check.py` | **Provider seam → needs the env below** |

The values are whatever the `re-com` MCP server is already configured with in
`~/.claude.json` (`mcpServers.re-com.env`) — read them from there rather than
guessing:

```bash
export RECOM_YTMUSIC_MCP_COMMAND=/path/to/ytmusic-mcp/.venv/bin/python
export RECOM_YTMUSIC_MCP_ARGS=/path/to/ytmusic-mcp/server.py
export YTMUSIC_AUTH_PATH=/path/to/ytmusic-mcp/headers_auth.json
```

For Spotify it is the `RECOM_SPOTIFY_MCP_*` pair plus `RECOM_PROVIDER=spotify`,
from `mcpServers.re-com-spotify.env`.

## Order matters — run in this sequence

```bash
# (prefix the Provider-seam scripts with the env above)
# 1. Crawl YouTube's editorial mood atlas (~35 min, resumable, safe to interrupt)
.venv/bin/python scripts/build_atlas.py

# 2. Label your library from that atlas + artist propagation (needs only YouTube Music creds)
.venv/bin/python scripts/label_library.py

# 3. Genre/language labels, for the language filter (~10-15 min)
.venv/bin/python scripts/build_genres.py

# 4. Tempo/BPM, from Deezer's public API (~0.4s per song)
.venv/bin/python scripts/build_tempo.py

# 5. Optional: cross-backend mood corpus via Deezer playlist search (works on any backend)
.venv/bin/python scripts/build_graph_atlas.py

# 6. Optional: have Claude read lyrics to cover what the atlas missed (needs an API key)
pip install -e ".[llm]"
.venv/bin/python scripts/label_library.py --claude
```

Step 5 (`build_graph_atlas.py`) is what makes mood tools work on Spotify at
all — the editorial atlas in step 1 is YouTube-only. If the user only cares
about YouTube Music, step 5 can be skipped, but skip it deliberately, not by
forgetting it.

## `refresh_library()` does NOT rebuild these indexes

Measured the hard way (2026-09-21): a store reporting `library: 1, labelled: 1`
with `trend_since_last_run.library_labelled: -1406` was *not* repaired by
calling the `refresh_library()` MCP tool. Two different subsystems:

| | what it is | what fixes it |
| --- | --- | --- |
| exclusion cache | `~/.recom/library_cache.json` — the "never recommend this" set | `refresh_library()` |
| library index | the `library` table in `store.db` — what mood seeds are drawn FROM | `.venv/bin/python scripts/label_library.py` |

A dead library index does not break the guarantee (exclusion still works), it
breaks *seeding*: every `recommend_for_mood` call returns `seeds: []` and falls
back to raw mood-playlist candidates, with the reason stated in `notes`. If
`index_status()` shows a library count far below the account's real size, run
`label_library.py` — `refresh_library()` will report success and change nothing
relevant.

## After changing `moodspace.ANCHORS`, re-materialize — don't re-crawl

Adding or moving an anchor (§7.17) changes where every already-crawled track
lands in the space, but not what was crawled. Both atlases recompute from
existing data in seconds:

```bash
.venv/bin/python scripts/build_graph_atlas.py --stage materialize
.venv/bin/python scripts/build_atlas.py   # materialize follows; the crawl resumes as a no-op
```

A *brand new* anchor also needs its `graph_atlas.MOOD_QUERIES` phrasings crawled
(`--stage crawl` is resumable and fetches only the new `(mood, query)` pairs) —
and an anchor with no phrasings gets no corpus at all, which is the failure
§7.17 exists to document. Finish with `--stage propagate` to write into the
provider store, or run the script with no `--stage` for all three.

## Checking progress without re-running anything

```bash
.venv/bin/python scripts/build_atlas.py --status
.venv/bin/python scripts/label_library.py --report
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

`get_history()` only reports "Today"/"Yesterday", so timestamps need a cron job,
not a one-time script. Note that `snapshot_history.py` is a Provider-seam script,
so the entry needs the same env as the table above — a bare crontab has none of
it, and the job will then die on startup every three hours without anyone
noticing:

```
0 */3 * * * cd /path/to/re-com && RECOM_YTMUSIC_MCP_COMMAND=/path/to/ytmusic-mcp/.venv/bin/python RECOM_YTMUSIC_MCP_ARGS=/path/to/ytmusic-mcp/server.py YTMUSIC_AUTH_PATH=/path/to/ytmusic-mcp/headers_auth.json .venv/bin/python scripts/snapshot_history.py >> /tmp/recom_history.log 2>&1
```

This feeds the implicit-feedback system (played/ignored inference) — it needs
no manual `record_feedback` calls to work, but it does need this cron running
continuously, unlike steps 1-6 above which are one-shot.
