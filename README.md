# re-com

An [MCP](https://modelcontextprotocol.io) server that recommends **new** songs — never a song already in your library, meaning never a song already in Liked Music *or in any of your playlists*, not just the one you seeded from.

It's built to do better than a streaming service's built-in radio/autoplay by pooling multiple independent discovery signals (radio, related content, artist catalog expansion, plus a service-neutral music graph) and ranking candidates by how many of them agree, instead of trusting one black-box algorithm.

**Discovery doesn't depend on any one service's API.** A streaming service can revoke the endpoints a recommender is built on, and Spotify did: for API apps registered after November 2024 without Extended Quota Mode, `/recommendations`, related-artists, artist-top-tracks and audio-features all return 403/404. Two of re-com's three original signals became unbuildable there and `recommend_from_song` returned **zero songs**. So similarity and adjacency now come from a neutral [music graph](#the-music-graph) (Deezer) that belongs to no backend, while the provider supplies only *whose taste this is* — library, history, playlist writes. Native signals are still used wherever they exist and still rank highest; they're just no longer required. See [The music graph](#the-music-graph) and `PLAN.md`'s v6 section.

**Backends: YouTube Music and Spotify.** re-com is a general recommendation engine, not tied to one service — re-com itself holds **no streaming-service credentials of any kind** for either backend. Every call goes through a sibling `*-mcp` server that re-com spawns as an MCP subprocess and that owns auth entirely: [`ytmusic-mcp`](https://github.com/umsachde/ytmusic-mcp) for YouTube Music, [`spotify-mcp`](https://github.com/umsachde/spotify-mcp) for Spotify. Which one a given re-com instance talks to is set once, at process start, via `RECOM_PROVIDER` — see [Setup](#2-connect-to-a-backend) below. Both are registered as separate MCP server instances (e.g. `re-com` and `re-com-spotify`); a single tool call always stays within one provider. See `provider.py` and `PLAN.md`'s "v3 — Multi-provider support" section for the design.

## Tools

| Tool | Description |
| --- | --- |
| `recommend_from_song(video_id=None, song=None, artist=None, limit=20, language=None, match_seed_tempo=False, ...)` | Recommend new songs similar to a seed song. Pass `video_id` directly, or `song` (optionally with `artist`). Supports [language](#language-filtering) and [tempo](#tempo-bpm) filters. Returns `{"songs": [...], "notes": [...], "filters": {...}}`. |
| `recommend_from_playlist(playlist_id, limit=20, seed_sample_size=5)` | Recommend new songs based on an entire playlist (samples seed tracks from it). |
| `songs_by_artist(artist, limit=10)` | Return actual songs by a named artist — a direct catalog pull, not a similarity recommendation. |
| `refresh_library()` | Force-rebuild the cached library exclusion set. See [Library cache](#library-cache). |
| `recommend_for_mood(feeling=None, vector=None, context=None, arc="mirror", limit=20, genres=None, language=None, bpm=None, ...)` | **v2.** Recommend new songs matching how you actually feel, shaped into a sequence that moves. See [Mood](#mood-aware-recommendations-v2). |
| `recommend_from_playlist_for_mood(playlist_id, feeling=None, vector=None, context=None, arc="mirror", limit=20, seed_cap=None, ...)` | **v2.** Mood *and* a playlist together: reads every track, seeds only from the ones that genuinely fit. See [Mood + one playlist](#mood--one-playlist). |
| `read_my_mood()` | **v2.** Infer your current mood from recent listening, *with the evidence for it*. |
| `explain_recommendation(video_id)` | **v2.** Why a song was picked, in mood terms. |
| `record_feedback(video_id, reaction)` | **v2.** `loved` / `saved` / `skipped` / `wrong_mood`. Rejections are never recommended again. See also [implicit feedback](#learning-without-being-told), which needs no call at all. |
| `index_status()` | **v2.** How much of the mood index exists, so gaps are visible instead of silent. |

All three tools guarantee every result is absent from Liked Music *and* from every one of your playlists, not just the one you seeded from (if any). `recommend_from_song` additionally never returns the seed song itself; `recommend_from_playlist` additionally never returns anything from the seed playlist even if that playlist somehow isn't in your library listing.

`songs_by_artist` is a different kind of tool from the other two: no scoring, no radio/related signals — just that artist's real catalog, with the same library-wide exclusion applied. It's a hard requirement, not best-effort: if fewer than `limit` qualifying songs exist, it returns however many were found (`found` in the response) rather than padding the list with substitutes. It never adds anything anywhere.

## Mood-aware recommendations (v2)

`recommend_from_song` answers *"what sounds like this?"*. `recommend_for_mood` answers a different
question: *"what does this person need to hear right now?"*

### Why this isn't just a filter

Running the v1 engine and filtering its results by mood does not work — filter a Daft Punk radio for
"melancholy" and you get the least danceable Daft-Punk-adjacent tracks, not melancholy music. So the mood
decides **where candidates come from**:

1. Resolve the mood to a vector.
2. Pick seeds from **your own library** that already sit near it.
3. Run v1's proven radio / related / artist expansion from those seeds.
4. Add a fourth signal: songs from YouTube's mood playlists near the target — the only path that reaches
   outside your existing taste graph.
5. Rank on signal agreement × mood fit, then **assign songs to slots along an arc**.

### The mood vector

| Axis | Range | Low ←→ high |
| --- | --- | --- |
| `valence` | −1…1 | despairing ←→ euphoric |
| `energy` | 0…1 | still ←→ frantic |
| `tension` | 0…1 | resolved ←→ anxious. **Separates angry from excited** — two axes can't tell aggressive workout rap from joyful party pop |
| `depth` | 0…1 | background wallpaper ←→ lyric-forward |

Pass `vector` for precision, `feeling` for free text (matched against a mood-word lexicon), or `context`
for one of YouTube's own moods. With none of them, the mood is inferred from your listening history.

### Arcs

A mood-matched *set* is the obvious thing to return and the wrong one. From music therapy's iso-principle:
to shift someone's mood you meet them where they are and move gradually — opening with upbeat songs when
someone is low just gets skipped.

| Arc | Behaviour |
| --- | --- |
| `mirror` | Stay where they are and validate it. Default. |
| `lift` | Start at their mood, rise gradually across the set. |
| `settle` | Descend to calm — an evening wind-down. |
| `deepen` | Go further in. |
| `hold` | Stay in a band with energy as a curve (a workout is warmup → peak → cooldown). |

### How a song's mood is known

YouTube Music exposes **no audio features at all** — no tempo, key, valence or energy (verified against
the live API; that's why BPM was dropped rather than built). So mood is assembled from four layers,
cheapest first, and the best available source for a song wins outright:

| Layer | What it is | Needs |
| --- | --- | --- |
| `llm` | Claude reads the lyrics. Handles any language, and irony. | Optional — `pip install -e ".[llm]"` |
| `lyrics` | Lyrics fetched and cached (2 API calls/song, incl. the negative result) | — |
| `atlas` | Membership in YouTube's own mood playlists — 1,592 listings, 65,438 tracks, 104,028 memberships | A crawl, YouTube only |
| `graph_atlas` | Membership in Deezer playlists found by mood search. [Works on any backend](#a-mood-corpus-that-works-on-any-backend) | A crawl |
| `artist` | An artist's average mood, propagated to their unlabelled songs | Free |

**The atlas alone is not enough, and measurably so.** On this account a 60-playlist sample covered 4.1% of
the liked library, and the misses concentrate on the Punjabi, Bollywood and Reggae catalogue that
YouTube's English-centric mood playlists barely touch. Artist propagation is what closes most of that gap
without any API key; the Claude layer closes the rest. The `graph_atlas` layer attacks the same gap from
the other side by searching for that catalogue by name, and unlike `atlas` it exists on every backend.

After a full crawl, measured: **71.3% library coverage** — 553 songs from artist propagation, 480 from
playlist membership.

### Mood + one playlist

*"I feel like this — look at this playlist and find me songs."*

`recommend_from_playlist` samples five tracks at random and ignores mood
entirely; `recommend_for_mood` honours the mood but draws seeds from the whole
library. `recommend_from_playlist_for_mood` is the intersection, and it treats
the playlist as evidence rather than as a bag to sample from:

1. **Every** track in the playlist is read and scored for mood fit.
2. Only *genuine* matches seed the search — a track whose mood can't be
   resolved, or that fits the target no better than an unlabelled song is
   assumed to, is not used. Seeding from tracks that don't fit would just hand
   back the playlist's own mood.
3. Seeds are spread across artists and capped (default 20, `seed_cap` to
   override). Each seed costs ~4 API calls, so a 100-song playlist would
   otherwise fire ~400.

`seed_report` says how many tracks were considered, how many were genuine, and
how many were capped away. If nothing fits, it says so and suggests
`recommend_for_mood` instead rather than returning off-mood results.

Exclusion is the same hard guarantee as everywhere else: nothing from Liked
Music, nothing from the seed playlist, nothing from any other playlist. The 25%
filler cap applies too.

### Turning a recommendation into a playlist

re-com is **read-only** — it never creates a playlist or adds a track anywhere.
That is deliberate: a recommendation engine that also mutates the library can't
be trusted to have excluded what it just added.

So "recommend me songs for this mood and make it a playlist" is two tools, in
this order:

1. `recommend_for_mood(...)` (or `recommend_from_playlist_for_mood(...)`) to get
   the songs.
2. A playlist-management tool — e.g. the separate `ytmusic` MCP server's
   `create_playlist` / `add_to_playlist` — to create it from the returned
   `videoId`s.
3. **`refresh_library()`**, so the tracks you just added are excluded from the
   next recommendation. Without this, the cached exclusion set is stale for up
   to `RECOM_CACHE_TTL` and a later call can recommend a song you just saved.

### Honesty about shortfalls

`limit` is a ceiling, not a guarantee. `recommend_for_mood`'s arc sequencer will
fill every requested slot from whatever's left in the candidate pool if you let
it, quality be damned -- asking for 100 with 7 songs that genuinely fit the mood
otherwise came back as 100, the other 93 being progressively worse guesses (an
unrated song still gets a placeholder fit score and can still win a slot).

Filler -- unrated, or rated but a poor fit -- is capped at 25% of `limit`.
Genuine matches (rated, with a real fit above the unrated baseline) are never
capped or dropped for this reason. Asking for 100 with 7 genuine matches
returns 32 (7 + 25), not 100. The result's `match_quality` field reports
`genuine`/`requested`/`fluff_cap`/`fluff_used`, and `notes` explains it in
plain language.

### Measuring quality

`scripts/quality_check.py` scores a fixed set of mood/arc cases so changes can be judged by number rather
than impression:

```bash
python scripts/quality_check.py --titles
python scripts/quality_check.py --distinctiveness 0   # A/B the seed scoring
```

Watch **cross-mood overlap**, not just mean fit. An early build scored a healthy 0.775 mean fit while
returning 70% the same songs for "heartbroken" and "angry"; fit alone couldn't see it. Current numbers:
mean fit 0.848, cross-mood overlap 0.064, 63 distinct songs across 80 slots.

### Learning without being told

`record_feedback` only fires when someone remembers to call it, which in practice is almost never — so
the engine also learns from what it can observe. Two tables it already keeps are enough: `recommendation`
(what was served, and when) and `history_log` (what was actually played, timestamped by
`scripts/snapshot_history.py`). Diffing them yields two signals for free:

| Inferred | When | Strength |
| --- | --- | --- |
| `played` | The song turned up in the history log *after* being recommended. | Strong — the recommendation landed. |
| `ignored` | It didn't, and ≥3 history snapshots have been taken since, so there was real listening it could have shown up in. | Weak, and treated as such. |

The threshold matters: below it, "not played" almost always means "the cron hasn't run yet" rather than
"they didn't want it". Songs under it are reported as `pending` and nothing is inferred.

**Inferred evidence never hard-excludes.** A stated `skipped`/`wrong_mood` bans a song permanently;
`ignored` only demotes, because absence from a history log has too many innocent explanations (they never
opened the playlist, they listened on another device). The two live in the same table under different
`source` values, and `rejected_video_ids` reads only the explicit ones.

What's learned is applied **per artist**, not per song — a song that got played usually gets liked, at
which point the library exclusion means it can never be recommended again anyway. What survives is the
direction it pointed in. The multiplier is bounded to 0.75–1.25 and saturates at 3 net reactions, so a
learned preference breaks ties without overruling signal agreement, and any nudge it applies is reported
in the result's `affinity` field rather than silently reordering things.

Inference runs automatically on every mood recommendation (pure local SQL, ~5ms, idempotent) — there's
nothing to schedule. `index_status()` reports what's accumulated so far.

### Setup

```bash
# 1. Crawl the mood atlas (~35 min, resumable, safe to interrupt)
python scripts/build_atlas.py

# 2. Label your library (steps 1-3 need no credentials beyond YouTube Music)
python scripts/label_library.py

# 3. Genre/language labels, for the language filter (~10-15 min)
python scripts/build_genres.py

# 4. Tempo, for BPM filtering (~0.4s per song)
python scripts/build_tempo.py

# 5. Optional: read lyrics with Claude to cover what the atlas missed
pip install -e ".[llm]" && ant auth login
python scripts/label_library.py --claude
```

Check progress any time with `python scripts/build_atlas.py --status`,
`python scripts/label_library.py --report`, or the `index_status()` tool.

Optionally, keep a real timeline of listening — `get_history()` reports only "Today"/"Yesterday", so
local timestamps are the only clock this system will ever have:

```
0 */3 * * * cd /path/to/re-com && .venv/bin/python scripts/snapshot_history.py
```

### Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `RECOM_DB_PATH` | `~/.recom/store.db` | Mood index, labels, history, feedback. [Scoped per backend](#one-store-per-backend) — Spotify uses `store-spotify.db`. |
| `RECOM_JUDGE_MODEL` | `claude-opus-5` | Model for lyric-based labelling. |
| `RECOM_JUDGE_EFFORT` | `low` | Effort level for that labelling. |
| `RECOM_JUDGE_BATCH` | `12` | Songs per labelling request. |
| `RECOM_SEED_WORKERS` | `6` | How many seeds are gathered concurrently. See [Speed](#speed). |
| `RECOM_GRAPH` | `1` | Set `0` to disable the [music graph](#the-music-graph) and use native signals only. |
| `RECOM_GRAPH_DB_PATH` | `~/.recom/graph.db` | The music-graph cache. **Shared by every backend** — not scoped per provider. |
| `RECOM_SPOTIFY_CAPABILITIES` | *(none)* | Comma-separated `radio,related,artist` to re-enable Spotify's native signals if your app has Extended Quota Mode. |

Everything mood-related is stored in local SQLite. The only thing that ever leaves the machine is,
optionally, song titles and lyric excerpts sent to the Claude API for labelling.

## Language filtering

*"Find songs like this Punjabi track, but only English ones."*

```python
recommend_from_song(song="Brown Munde", artist="AP Dhillon", language=["english"])
recommend_for_mood(feeling="hyped", exclude_languages=["punjabi", "hindi"])
```

Nothing in the YouTube Music API returns a language, so it's assembled in layers,
strongest first:

| Layer | Evidence | Weight |
| --- | --- | --- |
| `script` | Title written in Gurmukhi, Devanagari, Arabic, Hangul, Kana or Han | 100 |
| `library` | Your own playlist names (matched loosely — `Punjabu` counts) | 50 |
| `genre` | YouTube's genre-category pages | 10 |
| `genre` (English) | The same, but for anglophone genres | **1** |

**English is weighted at 1 on purpose.** YouTube files Punjabi and Hindi rap under
"Hip-hop", so counting an English-genre hit as a normal vote labelled Sidhu Moose Wala,
Karan Aujla and AP Dhillon as English. English is now what you get when *no*
language-bearing evidence exists, rather than something that can outvote real evidence.

Two behaviours worth knowing:

- **Unlabelled candidates are dropped by default.** Asking for English only is a request
  for a guarantee, and an unlabelled candidate from a Punjabi-seeded pool is probably
  Punjabi. The response always reports how many were dropped;
  `allow_unlabelled_language=True` keeps them.
- **Filtering alone isn't enough, so retrieval expands.** Seeding from a Punjabi song and
  filtering for English left 3 results out of 8 — the pool simply didn't contain more. The
  surviving songs are re-seeded to reach further into that language, and the response says
  when that happened. `expand_across_language=False` disables it.

This infers *language* from *genre*, which is approximate — "Dance & electronic" is often
instrumental, and "Reggae & caribbean" is usually English. Treat it as a strong hint.

## Tempo (BPM)

YouTube Music exposes no tempo data, so BPM comes from Deezer's public API — no key, no
auth, no attribution required.

```python
recommend_from_song(song="Kryptonite", artist="3 Doors Down", match_seed_tempo=True)
recommend_for_mood(context="Workout", bpm_min=120, bpm_max=140)
```

- `bpm` biases ranking toward a tempo; `bpm_min`/`bpm_max` bound it hard.
- `match_seed_tempo=True` uses the seed song's own BPM.
- **Half- and double-time count as close.** 170bpm drum-and-bass and 85bpm hip-hop share a
  pulse; treating them as opposites would be musically wrong.
- Tempo is **never propagated by artist**, unlike mood — an artist's songs share a
  sensibility, not a BPM. Propagating it would be inventing data.

**Coverage is uneven, and the response says so.** Measured across the whole library —
541 of 1,495 songs (36.2%):

| | | | |
| --- | --- | --- | --- |
| Rock & Alternative | 67% | Hip-Hop & Rap | 47% |
| R&B & Soul | 64% | Electronic & Dance | 38% |
| Pop | 60% | **Bollywood/Hindi** | **16%** |
| Country | 56% | **Punjabi** | **6%** |
| Reggae & Dancehall | 49% | | |

The misses are genuine: those songs resolve to the correct track on Deezer and simply carry
`bpm: 0`. So **a song with unknown BPM is never dropped**, only left unscored on tempo —
dropping them would quietly delete whole languages from the results.

Build the index with `python scripts/build_tempo.py` (~0.4s/song, cached permanently
including the misses).

## The music graph

Similarity, artist adjacency and the mood corpus come from [Deezer](https://developers.deezer.com/api)
— no key, no auth, no attribution — and belong to no backend. This is what makes re-com a
recommendation *app* rather than a wrapper around one service's algorithm.

| Signal | Source | Available on |
| --- | --- | --- |
| `radio` | the provider's per-track radio/autoplay queue | YouTube Music |
| `related` | the provider's per-track related-content feed | YouTube Music |
| `artist` | the provider's artist catalogue + related artists | YouTube Music |
| `graph_artist` | the seed artist's Deezer catalogue | every backend |
| `graph_radio` | Deezer artist radio | every backend |
| `graph_related` | adjacent artists' catalogues on Deezer | every backend |

Each backend declares what it can actually supply (`provider.capabilities()`), the engine runs whatever
is available, and ranking is unchanged: **a candidate scores by how many distinct signals agree on it.**
A backend with fewer native signals simply has fewer sources agreeing rather than returning nothing.
Measured live: Spotify went from 0 songs to 10; YouTube's top ten is unchanged and still native-dominated.

If Spotify ever grants your app Extended Quota Mode, set
`RECOM_SPOTIFY_CAPABILITIES="radio,related,artist"` to turn the native signals back on. Set `RECOM_GRAPH=0`
to disable the graph entirely and run on native signals alone.

**Known costs, stated plainly.** Deezer has no track-level radio (`/track/{id}/radio` doesn't exist), so
graph similarity is **artist-centric** — genuinely weaker than YouTube's per-track radio, which is why
native signals are added to rather than replaced. And the graph returns *"Diljit Dosanjh — Born to
Shine"*, not an id your backend understands, so candidates are matched back to the provider by search.
That resolution is **lazy**: results are ranked on graph metadata first and only the top of the pool is
ever resolved, so a fully-native response does none at all. A candidate that can't be matched is dropped
with a note rather than substituted.

### The graph cache is deliberately *not* per-backend

`~/.recom/graph.db` is shared by every provider instance — the exact opposite of
[the per-backend stores](#one-store-per-backend) below, and for the exact opposite reason. Deezer ids are
service-neutral: *"Excuses — AP Dhillon is Deezer track 1508646682"* is equally true for the YouTube
instance and the Spotify one. Scoping it per backend would resolve every artist twice and grow a third
copy on the next service. Override with `RECOM_GRAPH_DB_PATH`.

Negative results are cached alongside positive ones, so a song Deezer genuinely doesn't carry costs two
searches once rather than on every pass forever.

### A mood corpus that works on any backend

`recommend_for_mood` originally needed YouTube Music's editorial "Moods & moments" playlists, which is
why mood was YouTube-only. Deezer allows exactly what Spotify forbids — playlists can be *searched and
read* — so `scripts/build_graph_atlas.py` builds the same kind of evidence for every backend:

```bash
python scripts/build_graph_atlas.py                     # crawl, materialize, propagate
python scripts/build_graph_atlas.py --stage crawl --limit 20   # short trial run
```

The queries are deliberately not English-only. YouTube's mood playlists covered just **4.1%** of this
library's liked songs, with the misses concentrated on its Punjabi and Bollywood catalogue, so the neutral
atlas searches for that catalogue by name (`punjabi sad`, `bollywood romantic`, …). Moods are keyed by
Deezer id and inherited by provider tracks through the cached id bridge, so the labelling work is done
once no matter how many services you connect.

It ranks *below* the native atlas in `label.SOURCE_PRIORITY` — a playlist merely titled "sad songs" was
named by a stranger, where a YouTube mood playlist was filed by the service under a taxonomy — and above
artist propagation. Best available source still wins outright.

## One store per backend

Every id re-com persists — library rows, cached exclusion sets, mood labels, feedback — belongs to
exactly one backend's namespace, and they are **not interchangeable**: a YouTube videoId is 11
characters, a Spotify track id is 22. So each provider instance gets its own files:

| | YouTube Music (default) | Spotify |
| --- | --- | --- |
| Store | `~/.recom/store.db` | `~/.recom/store-spotify.db` |
| Exclusion cache | `~/.recom/library_cache.json` | `~/.recom/library_cache-spotify.json` |
| Music graph | `~/.recom/graph.db` | `~/.recom/graph.db` — **shared on purpose** |

The default backend keeps the original unsuffixed names, so an existing install keeps its crawled atlas,
labels and history rather than waking up to an empty store. `RECOM_DB_PATH` / `RECOM_CACHE_PATH` still
override outright if set.

The [music graph](#the-graph-cache-is-deliberately-not-per-backend) is the one deliberate exception:
Deezer ids belong to no service, so splitting that file would duplicate work without preventing any
mistake.

**This is a correctness guarantee, not tidiness.** Sharing one exclusion set between backends doesn't
merely mix the data — it silently voids the promise this project exists for, because no YouTube videoId
can ever equal a Spotify track id, so a 1,499-entry exclusion set matches *nothing* and every "new"
recommendation could already be in your library. Mood tools are refused outright on a backend with no
mood index rather than returning another provider's ids (see below).

## Speed

Two costs dominate a recommendation: building the library exclusion set (solved by the
[library cache](#library-cache) below) and gathering candidates from each seed.

Each seed costs ~4 sequential network round-trips, and a mood recommendation uses six seeds. Run
serially that's the sum of all six; nothing about it needs to be, since no seed depends on another and
the results are pooled regardless. Measured on the real account, same six seeds:

| | Serial | Concurrent |
| --- | --- | --- |
| Seed gathering | 18.9s | **3.1s** |
| `recommend_for_mood` end to end | ~18s | **5.7s** |

Concurrency is **capped** (`RECOM_SEED_WORKERS`, default 6) rather than unbounded: a playlist-seeded mood
request can carry 20 seeds, and 20 × ~4 simultaneous in-flight requests is exactly the rate-limit exposure
worth avoiding. Lower it if a backend starts throttling.

**Results are not identical run-to-run, and weren't before this either.** YouTube's radio is
non-deterministic — measured, two *serial* runs of the same seeds overlap only 0.793, while serial vs.
concurrent overlaps 0.819. Concurrency is not what varies the output; the API is.

## Library cache

Every recommendation excludes anything already in your library, which means building a set of every
videoId in Liked Music plus all of your playlists. Measured against a real account (~1,100 liked songs,
28 playlists, ~1,550 playlist tracks) that costs **~20 seconds** — and v1 paid it on every single tool call.

That set is now cached on disk. Measured on the same account:

| | Before | After |
| --- | --- | --- |
| Building the exclusion set | 20.5s | 0.9s |
| `recommend_from_song` end to end | ~24s | 4.3s |
| `songs_by_artist` end to end | ~22s | 2.6s |

**Liking a song still takes effect immediately.** A cache hit re-fetches only the most recently liked
songs (one page, ~1s) and unions them in, so the novelty guarantee holds for the mutation you actually
make most. The case a cache hit can miss is a song added to some *other* playlist within the TTL — call
`refresh_library()` after doing that if it matters, e.g. right after a playlist-management tool adds tracks.

If the top-up fetch fails, the cached set is used as-is rather than failing the call — a slightly older
exclusion set beats no recommendation, the same partial-results philosophy used for discovery signals.

| Env var | Default | Meaning |
| --- | --- | --- |
| `RECOM_CACHE_PATH` | `~/.recom/library_cache.json` | Where the cached set lives (~22 KB). [Scoped per backend](#one-store-per-backend). |
| `RECOM_CACHE_TTL` | `21600` (6 hours) | How long a cached set stays usable. **Set to `0` to disable caching** and rebuild on every call. |

The cache is written atomically (temp file + rename), and a missing, unreadable, malformed or expired
cache is treated as a miss rather than an error — worst case you pay the ~20s rebuild v1 always paid.

**Not included (v1):** BPM/tempo-based comparison. YouTube Music doesn't expose tempo data, so this needs a second data source (e.g. a third-party BPM API) — a stretch goal for a future version, not part of this build. See `PLAN.md` for the full design rationale.

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Connect to a backend

re-com holds no streaming-service credentials of its own for either backend — every call goes through a sibling `*-mcp` server that re-com spawns as a subprocess over MCP and that owns login entirely. Pick one (or set up both as two separate registrations):

#### YouTube Music (`ytmusic-mcp`)

1. Set up [`ytmusic-mcp`](https://github.com/umsachde/ytmusic-mcp) and authenticate it (see that project's own README) — this is the *only* place YouTube Music credentials live.
2. Point re-com at it via `RECOM_YTMUSIC_MCP_COMMAND` (its interpreter) and `RECOM_YTMUSIC_MCP_ARGS` (its `server.py` path). `RECOM_PROVIDER=youtube` is the default, so it doesn't need to be set explicitly.

```bash
claude mcp add re-com -s user \
  -e RECOM_YTMUSIC_MCP_COMMAND="/path/to/ytmusic-mcp/.venv/bin/python" \
  -e RECOM_YTMUSIC_MCP_ARGS="/path/to/ytmusic-mcp/server.py" \
  -- "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

#### Spotify (`spotify-mcp`)

1. Set up [`spotify-mcp`](https://github.com/umsachde/spotify-mcp) and authenticate it (see that project's own README) — this is the *only* place Spotify credentials live.
2. Register a **second, separate** re-com instance with `RECOM_PROVIDER=spotify` and `RECOM_SPOTIFY_MCP_COMMAND` / `RECOM_SPOTIFY_MCP_ARGS` pointing at it:

```bash
claude mcp add re-com-spotify -s user \
  -e RECOM_PROVIDER=spotify \
  -e RECOM_SPOTIFY_MCP_COMMAND="/path/to/spotify-mcp/.venv/bin/python" \
  -e RECOM_SPOTIFY_MCP_ARGS="/path/to/spotify-mcp/server.py" \
  -- "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

**What's different from YouTube Music, in practice — and it is severe.** Measured against a real app registration (2026-08-23), Spotify has revoked every discovery endpoint for apps created after November 2024 without Extended Quota Mode:

| Still works | Returns 403/404 |
| --- | --- |
| Saved tracks, playlists, recently played, top tracks/artists | `/recommendations` (404) |
| `search` (tracks, artists, playlists) | `artist_related_artists`, `artist_top_tracks` |
| `track`, `artist`, `artist_albums` → `album_tracks` | `audio_features`, `audio_analysis` |
| | Reading **any other user's playlist** |
| | `categories`, `featured_playlists`, `new_releases` |

**Two of the three discovery signals are therefore unbuildable on Spotify.** `spotify_client.py` degrades gracefully — a forbidden endpoint is skipped rather than failing the call — but graceful degradation of *every* signal is nothing, and before v6 `recommend_from_song` returned 0 results there. **v6 fixed that**: similarity and adjacency now come from the [music graph](#the-music-graph), which belongs to no backend, and `recommend_from_song` returns a full result set on Spotify (measured: 10 songs, 3.4s warm). Native signals are still preferred wherever the registration allows them — set `RECOM_SPOTIFY_CAPABILITIES` if your app has Extended Quota Mode.

`recommend_for_mood`, `recommend_from_playlist_for_mood` and `read_my_mood` **work on both backends as of 2026-08-29.** They were YouTube-only, because they need a mood index built from the service's own playlists and only YouTube has one; the neutral [graph atlas](#the-music-graph) now supplies that for any backend (measured: 40.2% mood coverage on a real Spotify library, against YouTube's editorial atlas at the same 40%). Wiring the graph into the mood path improved YouTube too — mean mood fit 0.797 → 0.820 and cross-mood overlap 0.121 → 0.096, with warm latency unchanged, since a fully-native result performs zero extra lookups. A backend with neither a native atlas nor graph coverage still **refuses with an explanatory error** rather than returning ids from the wrong namespace. Quality on Spotify is real but below YouTube's (0.201 cross-mood overlap vs 0.096, on a much smaller library); see `PLAN.md`, "The Spotify mood gate". `recommend_from_song`, `recommend_from_playlist`, `songs_by_artist`, `refresh_library`, `record_feedback`, `explain_recommendation` and `index_status` are available on both backends.

---

`-s user` makes either registration available in any Claude Code session, not just this directory. Use absolute paths throughout, since the server can be launched from any working directory.

For other MCP clients (Claude Desktop, etc.), point them at the same command and env vars using their respective config format.

If a backend's `*-mcp` auth expires or rotates, tool calls fail with a clear message pointing at re-authenticating *there* — re-com has nothing of its own to re-run.

**Offline maintenance scripts still authenticate directly.** `scripts/build_atlas.py`, `scripts/label_library.py`, `scripts/build_genres.py`, `scripts/build_tempo.py`, `scripts/snapshot_history.py`, and `scripts/quality_check.py` are indexing/labelling jobs you run yourself from the command line, not part of the live tool-call path — they still use `ytmusicapi` directly and need their own `headers_auth.json` (see `scripts/setup_auth_from_file.py` / `scripts/setup_auth.py`, and `RECOM_AUTH_PATH`). That's a separate, unrelated credential from `ytmusic-mcp`'s.

## Testing

Unit tests (`tests/`) cover the pure logic — normalization, scoring, ranking, exclusion filtering, library-cache behaviour (hits, misses, expiry, corruption, top-up, write failures), artist/song search resolution, error translation, and every tool end-to-end (happy path, signal failures, shortfalls, validation errors) — against a hand-rolled fake client matching `ytmusic_client.YTMusicClient`'s surface. `tests/test_spotify_client.py` and `tests/test_provider.py` cover `spotify_client.py`'s shape-translation logic (search/playlist/watch-playlist/related/artist/history normalization, graceful degradation when a restricted endpoint fails) and `RECOM_PROVIDER` backend selection the same way, against a fake `_call`. No network access, either `*-mcp` server, or any streaming-service credential required. A `conftest.py` fixture redirects the library cache to a temp path for every test, so runs never touch your real cache.

```bash
pip install -e ".[dev]"
pytest
```

Check coverage with:

```bash
pytest --cov=server --cov-report=term-missing
```

375 tests across the whole project. `tests/test_provider_isolation.py` covers the per-backend split —
path scoping, the default backend keeping its original filenames (so an existing install isn't orphaned),
explicit env overrides still winning, and that each mood tool refuses on a foreign backend *before*
reaching the provider while the v1 tools stay reachable. `tests/test_feedback.py` covers implicit feedback (the
recommendation/history diff, its idempotence, retraction of a wrong `ignored` verdict, and the guarantee
that inferred evidence never reaches the hard-exclusion set) and the bounded artist affinity it feeds.
`tests/test_concurrency.py` covers concurrent seed gathering — that it really is concurrent (proved with
a `threading.Barrier`, which can only clear if every seed is in flight at once, rather than a timing
assertion that could pass by luck), that it stays within the worker cap, and that each call site's
failure semantics are preserved. `tests/test_v2.py` covers the mood engine — the vector space, arcs, label resolution and artist propagation, the atlas crawler's resume and rate-limit behaviour, lyric caching, mood sensing, the Claude judge (against a fake client), and every v2 tool end to end (YouTube-only, per the mood engine's atlas dependency noted above). What remains uncovered is `_client()`'s real `YTMusicClient()`/`SpotifyClient()` construction (which actually spawns the sibling `*-mcp` subprocess) and the `if __name__ == "__main__"` entrypoint, neither meaningfully testable without a live connection.

`conftest.py` redirects both the library cache and the SQLite store to temp paths for every test, so runs never touch your real data.

`scripts/test_recommend.py` is a separate, complementary smoke test that talks to a real, running `ytmusic-mcp` (see Setup step 2) to sanity-check that the connection and live recommendations actually work.

## How recommendations are ranked

For each seed song, candidates are pulled from three independent signals:

1. **Radio** — YouTube Music's own autoplay/radio for that song.
2. **Related** — a separate "related content" signal, algorithmically distinct from radio.
3. **Artist expansion** — the seed artist's own other songs, plus top songs from a couple of their related artists.

A candidate's score is how many distinct (seed, signal) combinations surfaced it — the more independent signals agree, the higher it ranks. Every result includes a `sources` field showing which signals surfaced it, so recommendations are explainable rather than a black box.

**On Spotify**, the same three `sources` labels (`radio`/`related`/`artist`) are built from Spotify's own endpoints instead: `radio` from seed-track `/recommendations`, `related` from the seed artist's related artists' top tracks, and `artist` from the seed artist's own top tracks (plus a couple of related artists', same as YouTube Music). See `spotify_client.py` for the mapping and its limitations (no full-catalog endpoint, and `/recommendations`/related-artists may be 403'd for newer Spotify API apps).

Liked Music and every playlist in your library are excluded last, always, as a hard filter — no recommendation can ever be a song you've already liked or already saved anywhere.

## Error handling

Tool calls translate common failure modes into clear messages instead of raw tracebacks:

- Missing/expired/malformed YouTube Music auth, rate limiting, gated/restricted content, and network errors are all translated by `ytmusic-mcp` itself (re-com has no auth of its own to point at) — its message tells you what to do, e.g. re-authenticate *there*.
- If `ytmusic-mcp` isn't reachable at all (not configured, or the subprocess won't start), re-com says so plainly rather than hanging.
- If an individual signal (radio, related, or artist expansion) fails for a given seed, that signal is silently skipped for that seed rather than failing the whole recommendation.

## License

MIT — see [LICENSE](LICENSE).
