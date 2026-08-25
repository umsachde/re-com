# re-com v2 — Mood-Aware Recommendation

> Design doc for v2. Read alongside `PLAN.md` (v1 design + v3 multi-provider notes).
> Every number in "Measured ground truth" below was probed against the real account on 2026-08-19,
> not estimated. Re-probe before trusting them after a few months.

## Thesis

v1 answers *"what is similar to this seed song?"* It's a similarity engine with a novelty guarantee.

v2 answers a different question: **"what does this person need to hear right now?"**

That is not a filter bolted onto v1. Similarity is a property of a *song pair*; mood is a property of a
*person at a moment*. The engine needs to model both, and the mood side needs three things v1 has none of:

1. A **mood representation** the tools, the index, and Claude can all speak.
2. **Mood knowledge about songs** — YouTube Music exposes no audio features whatsoever, so this must be built.
3. **Mood knowledge about the user** — their state right now, and their personal dialect of words like "chill."

BPM was the v1 stretch goal. It should be *demoted*, not built. See "Why not BPM" below.

---

## Measured ground truth

Probed live against the account (`headers_auth.json`) on 2026-08-19.

### What YouTube Music gives us

| Source | Result | Verdict |
| --- | --- | --- |
| `get_song(videoId)` | `videoDetails` has only title, author, `lengthSeconds`, `viewCount`, `musicVideoType` | **No audio features. None.** No tempo, key, valence, energy, danceability. |
| `get_mood_categories()` | 13 moods & moments, 27 genres | **The unlock.** First-party mood taxonomy, free. |
| `get_mood_playlists(params)` | **2,223 playlists** across the 13 moods (4.4s to enumerate all 13) | A large, free, mood-labeled corpus. |
| `get_lyrics(browseId)` | Works. Sample track returned 1,463 chars of real lyrics | **The deepest signal available.** Emotional content in plain text. |
| `get_history()` | 200 items, newest-first, with `videoId`, `likeStatus`, `inLibrary`, `duration` | Mood-sensing input — with a caveat, below. |
| `get_tasteprofile()` | 1,196 artists | Taste breadth, weak mood value. |

Mood playlist distribution — this is the corpus v2 is built on:

```
Chill      358    Commute    379    Energize   314    Feel good  220
Party      205    Romance    150    Focus      148    Christmas  131
Halloween  113    Workout     86    Sleep       51    Sad         41
Gaming      27
                                            TOTAL: 2,223 playlists
```

### The user's library

| | |
| --- | --- |
| Liked Music | ~1,100 tracks (7.4s to fetch) |
| Playlists | 28, holding 1,554 tracks (12.0s to fetch all) |
| Unique corpus | ~2,300–2,600 tracks |
| Genre buckets already curated by hand | `C - Bollywood/Hindi`, `C - Punjabi`, `C - Country`, `C - Hip-Hop & Rap`, `C - Electronic & Dance`, `C - Pop`, `C - R&B & Soul`, `C - Reggae & Dancehall`, `C - Rock & Alternative`, `C - Other / Mixed` |

**Every single v1 tool call rebuilds that exclusion set from scratch — ~20 seconds of API calls before any
recommendation work begins.** That is a v2 blocker regardless of mood, and Phase 0 fixes it.

### The coverage probe — the finding that shapes everything

Crawled a random 60 of the 2,223 mood playlists (2.7% of the atlas, 4,531 unique tracks, 41s) and
intersected against the ~1,100 liked tracks:

```
Coverage from a 2.7% sample:  43 / 1,061 liked tracks  =  4.1%
Mood labels that landed:      Romance 14, Feel good 14, Chill 9, Party 3,
                              Focus 1, Energize 1, Sleep 1, Sad 1
Tracks carrying >1 label:     1
```

Two conclusions, and they set the whole architecture:

1. **A full crawl will not cover this library.** Coverage grows sublinearly (popular tracks repeat across
   playlists), and the misses will concentrate exactly where this user listens most — Punjabi, Bollywood,
   Reggae/Riddim. YouTube's mood playlists are heavily English-language-pop-centric. Realistic expectation
   after a full crawl: **30–60% of this library, with the gap non-random.** Phase 1 must measure this for
   real and gate on it.
2. **Playlist membership alone yields ~1 mood label per track.** That gives you anchor-snapping, not a
   nuanced mood vector. A track tagged only "Chill" is indistinguishable from every other "Chill" track.

Therefore the lyrics + LLM layer is **not a nice-to-have refinement — it is load-bearing.** The atlas is
the cheap broad prior; language understanding is what actually produces mood resolution and covers the
non-English catalog. Plan accordingly.

---

## The mood model

A flat tag list ("sad", "hype", "chill") can't do distance, blending, or trajectories. Use a small
continuous vector so mood becomes geometry.

### Axes

| Axis | Range | Low end ←→ High end | Why it earns a slot |
| --- | --- | --- | --- |
| `valence` | -1..1 | despairing ←→ euphoric | The primary emotional sign. Russell's circumplex. |
| `energy` | 0..1 | still ←→ frantic | Arousal. The other circumplex axis. |
| `tension` | 0..1 | resolved/warm ←→ anxious/aggressive | **Separates angry from excited.** Both are high-energy, low-and-high valence respectively, but "aggressive workout rap" and "joyful party pop" are not interchangeable, and 2 axes can't tell them apart. |
| `depth` | 0..1 | wallpaper ←→ lyric-forward, demands attention | Decides whether the user gets something to *think along with* or something to work behind. Focus vs. Sad both matter here. |

Plus two non-mood knobs the mood state *sets*:

- `familiarity_appetite` (0..1) — comfort vs. novelty. Low mood usually wants the known; this is why a pure
  discovery engine can feel hostile when someone's down. v1 is hard-wired to maximum novelty; v2 must be
  able to dial it back, which means **allowing already-known songs into results when the mood calls for it**
  — a deliberate, opt-in relaxation of v1's central guarantee, never the default. (See "Open decisions".)
- `context` (enum) — `workout | focus | commute | sleep | party | romance | driving | none`. Maps almost 1:1
  onto YouTube's own "Moods & moments", so it is free structurally.

### Anchors

Each of the 13 YT moods gets a hand-authored anchor vector. First draft, to be tuned against real data:

```
                valence  energy  tension  depth
Sad              -0.70    0.25     0.35    0.85
Chill             0.25    0.25     0.15    0.35
Sleep             0.10    0.05     0.05    0.15
Focus             0.05    0.35     0.20    0.10
Commute           0.30    0.55     0.30    0.40
Feel good         0.75    0.60     0.10    0.35
Romance           0.55    0.35     0.20    0.70
Energize          0.60    0.90     0.45    0.30
Workout           0.35    0.95     0.70    0.20
Party             0.70    0.85     0.30    0.15
Gaming            0.10    0.75     0.65    0.15
```

A track's atlas vector = confidence-weighted mean of the anchors of every mood playlist it appears in.
Confidence = number of distinct mood *playlists* (not moods) it was found in.

### Free sub-mood signal

Playlist *titles* inside a mood are themselves fine-grained labels. The "Sad" mood contains
`Burn the Photos`, `Country Breakup`, `Hip Hop Heartbreak`, `End of the Road: Classic R&B Breakup`,
`Deal With It`. That is heartbreak-vs-resignation-vs-defiance resolution, for free, from strings we already
fetch. Feed the titles to the LLM labeler as context rather than throwing them away.

---

## Four layers of song-mood knowledge

Layered cheapest-and-broadest first; each layer fills the one above's gaps. Every result is cached
permanently by `videoId` — a song's mood does not change.

### Layer 1 — The Mood Atlas (offline, free, broad)

Crawl `get_mood_categories()` → `get_mood_playlists()` → `get_playlist()` for all 2,223 playlists.
Store `videoId → {mood, playlist_title, playlist_id}` rows.

- Cost: ~2,250 API calls. At the measured 0.7s/playlist, **~25–45 minutes** with throttling.
- Run as a background script (`scripts/build_atlas.py`), resumable, checkpointed after every playlist,
  refreshed monthly. Never inline in a tool call.
- Rate limiting is the live risk — 2,000+ sequential calls is exactly what earns a 429. Throttle
  deliberately (1 req/s, jittered), honour `Retry-After`, and make resume-from-checkpoint a first-class
  path, not an afterthought.

**Gate:** after the first full crawl, print real coverage against the library. If it lands under ~35%,
Layer 1 is a weak prior rather than a backbone, and Phase 2 gets prioritised harder.

### Layer 2 — Lyrics

`get_watch_playlist(videoId)` returns a `lyrics` browseId → `get_lyrics()` returns plain text. Confirmed
working.

- Cost: **2 API calls per song.** Never do this for 300 candidates inline. Do it for (a) the user's own
  library as a one-time background pass, and (b) the top ~30 shortlisted candidates in a request.
- Not universally available: instrumentals, and expect thinner coverage on the regional catalog.

### Layer 3 — The LLM judge

Title + artist + genre + atlas labels + playlist titles + lyric excerpt → mood vector JSON.

- Model split: `claude-haiku-4-5` for bulk library labeling, `claude-sonnet-5` for the live shortlist
  where precision actually shows.
- This is what handles Punjabi and Hindi lyrics, irony, and songs the atlas never saw — i.e. exactly the
  gap the coverage probe exposed.
- Batch 20–40 tracks per request. One-time cost per song, cached forever. A full ~2,500-track library pass
  on Haiku is small — low single-digit dollars.
- **Must degrade gracefully with no API key**: fall back to atlas-only, and say so in the tool response
  rather than silently returning worse results.
- Optional cheap fallback instead of an LLM: the NRC VAD lexicon (~20k English words rated for
  valence/arousal/dominance). No API, no cost, no network — but English-only and blind to irony, so it
  cannot close the gap that matters here. Fine as a stopgap; not a substitute.

### Layer 4 — The personal mood map

Label the user's own ~2,500-track library with Layers 1–3, once, in the background. This produces the
thing that actually makes recommendations feel personal:

- **Their** "chill" centroid, not the generic one. When they say chill, resolve against what *they*
  actually play when chilled out.
- The hand-curated `C - *` playlists give a **genre prior** per track for free — which answers the open
  question in `PLAN.md` about cross-genre noise. A mood match that jumps from Punjabi to Christian gospel
  is technically correct and practically wrong; genre affinity becomes a ranking term.
- Enables mood-matched seeding, below — the single biggest quality lever in this plan.

---

## Mood sensing — reading the user

Three inputs, fused, in strict priority order.

### 1. What they say (highest weight)

Claude is already in the loop and is a far better mood parser than anything shipped in the server. The tool
contract should exploit that rather than duplicate it:

```
recommend_for_mood(
    feeling: str | None,        # free text, verbatim from the user
    vector: dict | None,        # Claude's structured read, if it wants to be explicit
    context: str | None,        # workout | focus | commute | ...
    ...
)
```

Accept both. `feeling` keeps the tool usable by any client; `vector` lets Claude pass a nuanced read
("wistful but wants to stay productive" → high depth, low-ish valence, mid energy, low tension) without
round-tripping through a lossy string. If both arrive, `vector` wins and `feeling` is retained for logging.

### 2. What they've been playing

`get_history()` gives 200 items, newest-first. Compute the mood centroid and the *trajectory* of recent
plays, then detect the patterns that actually mean something:

- Same song repeating → strong emotional signal, and the single most reliable one available.
- Valence drifting down across a session → they're sinking.
- A sharp energy spike → gearing up.
- One artist dominating (the probe showed a Joyner Lucas run) → they're in a specific headspace, and it
  has a name.

**Real limitation, stated plainly:** `played` is only `"Today"` / `"Yesterday"` — coarse buckets, no
wall-clock timestamps. You get *order*, not *hour*. Fix by snapshotting history on a schedule and stamping
observations with local time, which accumulates a genuine longitudinal log the API refuses to give.

### 3. Context

Local time of day and day of week, as a weak prior only. 7am Tuesday and 11pm Friday are different
requests even with identical words. Never let this override what the user actually said.

### The tool that makes this conversational

`read_my_mood()` returns the inferred state **with its evidence**, so Claude can open with:

> "You've had the same three Joyner Lucas tracks on loop since yesterday — that reads pretty heavy.
> Want something that sits there with you, or something that pulls you up?"

That exchange *is* the product. It's the difference between a recommender and something that feels like it
noticed. Ship the evidence, not just the verdict.

---

## Retrieval — mood-matched seeding

The lazy version of v2 is: run v1, then filter by mood. **Don't build that.** It fails badly — filtering a
Daft Punk radio for "melancholy" yields the least-danceable Daft Punk adjacent tracks, not melancholy music.

Instead, mood changes *where candidates come from*:

1. Resolve target mood → vector.
2. **Pick seeds from the user's own library nearest that vector** (Layer 4), 5–8 of them, weighted by their
   genre distribution so results don't collapse into one bucket.
3. Run v1's proven 3-signal generation per seed (radio / related / artist expansion) — unchanged, it works.
4. **Add a 4th signal: mood-playlist neighbours.** Pull tracks from atlas playlists whose vector is near the
   target. This is the discovery path that reaches outside the user's existing taste graph, which is exactly
   what radio-from-your-own-songs can never do.
5. Score = v1's convergence score **× mood fit × genre affinity × novelty weight**, with each term
   reported separately so results stay explainable.
6. Apply v1's library exclusion — unless `familiarity_appetite` is high and the user opted into comfort mode.

Step 2 is the heart of it. "I'm feeling nostalgic" should seed from the songs *this person* finds nostalgic,
which is a fact the system can only know because Layer 4 exists.

---

## Arcs — the differentiating feature

Don't return a mood-matched *set*. Return a mood-shaped *sequence*.

The iso-principle from music therapy: to move someone's affect, meet them where they are and shift
gradually. Jumping straight to upbeat songs when someone is low gets skipped — it reads as being told to
cheer up.

| Arc | Behaviour |
| --- | --- |
| `mirror` | Stay at their current mood. Validate it. The default when someone names a feeling. |
| `lift` | Start at current, interpolate to higher valence/energy across N tracks. |
| `settle` | Start at current, descend to calm. Evening wind-down. |
| `deepen` | Move further in. Sometimes you want to sit in it properly. |
| `hold` | Stay inside a context band, shaping energy as a curve — workout is warmup → peak → cooldown, not a flat wall of intensity. |

Implementation: compute a target vector per slot along the curve, then assign the best-fitting candidate to
each slot — greedy is fine to start, Hungarian assignment if it proves lumpy. Add sequencing constraints:
no same artist adjacent, no jarring energy jump between neighbours, cap tracks per artist across the set.

No streaming service's radio does this. It is the clearest reason for v2 to exist.

---

## Feedback and learning

Log every recommendation with the mood context it was served under, then close the loop.

**Explicit:** a `record_feedback(video_id, reaction)` tool — `loved | saved | skipped | wrong_mood`.
`wrong_mood` is the valuable one; it says the retrieval was fine and the *mood model* was off.

**Implicit, and better because it costs the user nothing:** a background job diffs prior recommendations
against subsequent `get_history()`. Did a recommended track get played? Played repeatedly? Liked? Added to
a playlist? That's ground truth with zero friction, and it's available precisely because history is
readable.

What the loop adjusts:
- Personal anchor vectors drift toward what they actually accept in each mood.
- Artists repeatedly rejected in a given mood get downweighted *for that mood*, not globally.
- Per-mood novelty tolerance is learned rather than assumed.

---

## Architecture

v1 is a single ~500-line `server.py` with no persistence. v2 needs real structure — and it happens to be
the same restructuring `PLAN.md`'s v3 multi-provider work needs, so do it once, properly.

```
recom/
  server.py            # thin MCP tool layer only
  provider/
    base.py            # Protocol — the v3 seam, defined now
    ytmusic.py         # everything ytmusicapi-specific
  mood/
    space.py           # vectors, axes, distance, interpolation
    anchors.py         # the 13 mood anchor vectors
    atlas.py           # crawl + query the mood-playlist index
    lyrics.py          # fetch + cache
    judge.py           # LLM labeling (degrades gracefully without a key)
    sense.py           # history + context → current mood
  rank.py              # scoring, now multi-term
  arc.py               # sequencing
  store.py             # SQLite
scripts/
  build_atlas.py       # ~30 min, resumable, cron-able
  label_library.py     # one-time + incremental
  snapshot_history.py  # timestamps history; cron every few hours
```

### Storage — SQLite at `~/.recom/store.db`

| Table | Holds |
| --- | --- |
| `track` | videoId, title, artists, album, duration, genre_prior |
| `track_mood` | videoId, valence, energy, tension, depth, confidence, source (`atlas`/`lyrics`/`llm`), labeled_at |
| `atlas_membership` | videoId, mood, playlist_id, playlist_title |
| `library_snapshot` | the exclusion set + fetched_at — **shipped ahead of the rest, as a JSON file rather than SQLite; fold it in when this table lands** |
| `history_log` | videoId, observed_at (real local timestamp), position |
| `recommendation` | videoId, served_at, mood context, arc, slot, score terms |
| `feedback` | videoId, reaction, source (`explicit`/`inferred`), at |

Cache the exclusion set with a TTL (~6h) plus an explicit `refresh_library()` tool. This alone makes every
existing v1 tool roughly 20 seconds faster, and is worth shipping before any mood work lands.

---

## Tool surface

v1's three tools keep working unchanged — no breaking changes.

**New:**

| Tool | Purpose |
| --- | --- |
| `recommend_for_mood(feeling, vector, context, arc, limit, familiarity, genres)` | The headline tool. |
| `read_my_mood()` | Inferred current mood **plus the evidence for it**. Makes the conversation possible. |
| `explain_recommendation(video_id)` | Why this song, in mood terms — extends v1's `sources` honesty. |
| `record_feedback(video_id, reaction)` | Close the loop. |
| `index_status()` | Atlas/label coverage and freshness, so failures are visible instead of silent. |

**Extended:** `recommend_from_song` / `recommend_from_playlist` gain an optional `mood` filter, so "more like
this, but calmer" works.

---

## Why not BPM

The v1 plan listed BPM as the v2 stretch goal. It should be dropped to the bottom, for three reasons:

1. **YouTube Music has no tempo data at all** — confirmed by probing `get_song()`. It requires a whole
   third-party integration to obtain.
2. **The obvious sources are weak.** AcousticBrainz stopped collecting data in 2022 and is effectively
   frozen; matching YouTube tracks to MusicBrainz recording IDs is lossy, and coverage of Punjabi/Bollywood
   catalog will be poor. GetSongBPM covers tempo but tempo is not mood.
3. **BPM is a bad mood proxy anyway.** 140bpm covers both a rage track and a euphoric one. Tension and
   valence are what distinguish them, and lyrics carry that; tempo does not.

The user's own framing — *"more than just seeing the BPM"* — is correct, and the probe data backs it.
If tempo is still wanted later, it belongs as a **sequencing** input (smoothing transitions inside an arc),
not as a mood signal.

---

## Phasing

Each phase is independently shippable and useful on its own.

| Phase | Scope | Why here |
| --- | --- | --- |
| **0 — Foundation** | ~~Cached exclusion set~~ **(done)**; package split, SQLite store, provider seam still open | Unblocks everything. The cache shipped first and is measured at 20.5s → 0.9s; see `PLAN.md` build status. |
| **1 — Atlas + mood space** | `build_atlas.py`, anchors, `recommend_for_mood` v0 (atlas-only) | First real mood recommendations, no API key needed. **Gate: measure true library coverage.** |
| **2 — Language layer** | Lyrics fetch + LLM judge + caching | Closes the coverage gap the probe found. Where mood resolution actually becomes good. |
| **3 — Personal map** | Label the library, mood-matched seeding, genre affinity | The step where results start feeling personal rather than correct. |
| **4 — Sensing** | `read_my_mood`, history snapshot cron, context priors | Enables the conversational opening. |
| **5 — Arcs** | Sequencing, the 5 arc types | The differentiator. Needs 1–3 to be solid first. |
| **6 — Learning** | Feedback tools, implicit history diffing, anchor drift | Compounds only after there's usage to learn from. |

---

## Risks

- **Rate limiting.** 2,200+ sequential playlist fetches is the most likely thing to break. Throttle, jitter,
  checkpoint, resume. Never crawl inline.
- **Coverage gap is non-random.** Punjabi/Bollywood/Riddim are underserved by YT's mood playlists *and* by
  English lyric lexicons. This is the plan's main quality risk, and Phase 2 is the mitigation.
- **Auth expiry breaks background jobs silently.** Header auth rotates. Every cron script needs a health
  check and a visible failure path; `index_status()` should surface staleness.
- **Cold start.** Phases 1–3 need a full crawl and a library labeling pass before quality shows. Budget
  roughly an hour of background compute before judging results.
- **Mood inference will sometimes be wrong.** Design for it: always show evidence, always let the user
  correct, never assert a feeling as fact. "That reads pretty heavy — right?" not "You are sad."
- **Privacy.** Lyrics, history and mood inferences are personal. Everything stays in local SQLite; the only
  thing that ever leaves the machine is (optionally) titles and lyric excerpts to the Claude API for
  labeling. Make that opt-in and documented.

---

## Open decisions

Three forks that materially change the build. Recommendations given; user's call.

1. **Allow the LLM labeling layer?** It costs a few dollars one-time and sends song titles and lyric
   excerpts to the Claude API. *Recommendation: yes* — the coverage probe shows the atlas alone cannot
   carry this specific library. Keep it optional and degrade to atlas-only without a key.
2. **Third-party APIs (Last.fm tags, GetSongBPM)?** Last.fm crowd tags would add a genuinely independent
   mood signal with good long-tail coverage. *Recommendation: defer* — YT atlas + lyrics + LLM likely
   suffices, and each integration adds an auth surface and a failure mode. Revisit if Phase 2 disappoints.
3. **Stay read-only?** Arcs are ordered sequences, which want to become real playlists. v1 is deliberately
   read-only, with writes delegated to `ytmusic-mcp`. *Recommendation: stay read-only* — return the ordered
   list and let the orchestrator write it via `ytmusic-mcp`. Preserves the clean separation and v3 portability.

---

## Build status (2026-08-19)

**Shipped.** Phases 0-5 are implemented, tested and verified against the real account. Phase 6 (learning)
is partially in: feedback is recorded and rejections are enforced, but nothing yet drifts the anchors.

| Phase | State |
| --- | --- |
| 0 — Foundation | **Done.** Library cache (20.5s → 0.9s), SQLite store, `signals.py` extracted so v1 and v2 share candidate generation. |
| 1 — Atlas + mood space | **Done.** `moodspace.py` (4 axes, 11 anchors), `atlas.py` + `scripts/build_atlas.py` (resumable, rate-limit aware). |
| 2 — Language layer | **Built, not run.** `lyrics.py` caches lyrics incl. negative results; `judge.py` labels via Claude. Optional dependency — not installed on this machine, so unexercised against the live API. |
| 3 — Personal map | **Done.** `label.py` — source priority, artist propagation, genre priors from the `C - *` playlists, library sync. |
| 4 — Sensing | **Done.** `sense.py` + `read_my_mood()` + `scripts/snapshot_history.py`. |
| 5 — Arcs | **Done.** `arc.py` — five arcs, slot assignment, per-artist caps. |
| 6 — Learning | **Partial.** `record_feedback()` and rejection enforcement ship; anchor drift and implicit history-diffing do not. |

### What the measurements actually said

The coverage worry in this plan was correct, and the mitigation mattered more than expected:

- **Atlas alone is thin.** At 34% of the crawl the atlas had placed 44,310 songs but accounted for only
  205 of the library's labels.
- **Artist propagation is doing the heavy lifting** — 551 of 756 labels, and it needs no API key at all.
  It was added as a hedge against the LLM layer being unavailable; it turned out to be the main engine of
  coverage.
- **Library coverage reached 52.2% with the crawl only a third done** and no Claude labelling whatsoever —
  ahead of the 30-60% range predicted here.

Re-measure with `python scripts/label_library.py --report` once the crawl finishes.

### Deviations from the plan above

- **Flat modules, not a `recom/` package.** The repo was already flat (`server.py` at the root);
  mixing layouts mid-build would have been worse than either choice. Revisit alongside the v3 provider
  work, which needs the same restructuring.
- **The v1 library cache stayed a JSON file** rather than folding into `library_snapshot`. It works and is
  tested; rewriting it purely for storage uniformity wasn't worth the churn.
- **`claude-opus-5` is the default judge model, not Haiku.** Which model reads your library is a quality
  decision that belongs to the user; `RECOM_JUDGE_MODEL` exists for anyone who wants to trade
  accuracy for cost.
- **Genre is a ranking input, not a mood source.** Genre says nothing about mood on its own, so it filters
  seeds and boosts known artists rather than pretending to place a song in the space.

### Known gaps

- ~~**`recommend_for_mood` takes ~18s.**~~ **Done (2026-08-23)** — see below.
- **The Claude labelling path has never run against the real API** — only against a fake client in tests.
- **Anchor vectors are hand-authored first drafts.** They have not been tuned against real listening.
- ~~**No implicit feedback yet.**~~ **Done (2026-08-23)** — see below.

---

## 2026-08-23 session — the two remaining gaps above, closed

### Implicit feedback

`record_feedback` only fires when someone remembers to call it, which in practice is never — the explicit
feedback table had **1 row** on the real account after weeks of use, against 156 recommended songs. But
the two tables needed to infer the same thing were already accumulating: `recommendation` (what was
served, when) and `history_log` (what was played, timestamped by the cron'd `snapshot_history.py`).

`store.infer_implicit_feedback` diffs them. Design decisions worth keeping:

- **Inferred evidence never hard-excludes.** This is the whole reason `played`/`ignored` are separate
  reaction names rather than reusing `skipped`. A permanent ban is the strongest thing this system can do
  to a song and "didn't show up in the history log" has too many innocent explanations — they never
  opened the playlist, they listened elsewhere, the cron missed a window. `rejected_video_ids` grew an
  explicit-only source guard so a future inference can't silently start banning things either.
- **A "not played" verdict needs ≥3 snapshots to have been taken since.** Below that, absence means "the
  cron hasn't run" far more often than "they didn't want it". Songs under the threshold report as
  `pending` — on the real account 149 of 156 were pending, exactly as expected with only 2 snapshots
  recorded so far. **The signal is real but slow**, and that's inherent: it grows with the cron, not with
  usage.
- **Retraction, not just accumulation.** A song inferred `ignored` that later gets played had its
  inference proven wrong; the row is deleted rather than left as a stale penalty beside contradicting
  evidence.
- **Applied per artist, not per song.** A played song usually gets liked, at which point the library
  exclusion means it can never be recommended again — so the song-level signal is nearly worthless and
  the direction it pointed in is the whole value. Bounded 0.75–1.25, saturating at 3 net reactions:
  this is mostly *inferred* evidence and it should break ties, not overrule retrieval. Surfaced as
  `affinity` on the result so a nudge is explainable rather than an unexplained reordering.

First live run: 156 songs considered, 7 `played` inferred, 0 `ignored` (correctly held back by the
snapshot threshold), 7 artists with a learned affinity.

### Concurrent seed gathering

The ~18s was six seeds × ~4 sequential round-trips. No seed depends on any other and `_merge_and_score`
pools them regardless, so this was serial only because it was written that way.

`signals.gather_seeds` replaces three separate serial loops (`recommend.build`, `recommend.bridge_expand`,
`server.recommend_from_playlist`). Measured on the real account, same six seeds: **18.9s → 3.1s (6.1x)**,
and `recommend_for_mood` end-to-end **~18s → 5.7s**.

- **Threading is safe here for a specific reason**, not by luck: a `Provider` is a synchronous facade over
  one asyncio loop on a dedicated thread, calls go through `run_coroutine_threadsafe`, and the MCP session
  multiplexes concurrent requests over stdio by request id. N caller threads blocking on N in-flight calls
  is the shape it was already built for.
- **Concurrency is capped (`RECOM_SEED_WORKERS`, default 6), not unbounded.** A playlist-seeded mood
  request carries up to 20 seeds; 20 × ~4 simultaneous requests is precisely the rate-limit exposure
  PLAN.md flagged as a v2 concern. The cap keeps the common case fully parallel while bounding the worst.
- **Failure semantics differ per call site and are now explicit.** `skip_failures=True` (the mood paths)
  drops a dead seed; `skip_failures=False` preserves `recommend_from_playlist`'s existing contract of
  surfacing the error. Note this guard is for *unexpected* crashes — provider errors are already absorbed
  per-signal one level down, so a seed whose signals all fail returns `{}` rather than raising.
- **A false alarm worth recording.** Serial and concurrent runs returned different candidate sets, which
  looked like a threading bug. It isn't: two *serial* runs of the same seeds overlap only **0.793**, while
  serial vs. concurrent overlaps **0.819** — YouTube's radio is non-deterministic per call. Checking
  serial-against-serial before blaming the change is the general lesson; the same trap is available for
  anything measured against this API.
- Tests prove concurrency with a `threading.Barrier` (which can only clear if every seed is in flight
  simultaneously) rather than a wall-clock assertion that could pass by luck on a fast machine.

---

## Measured results (2026-08-19, crawl complete)

The atlas crawl finished: **1,592 playlist-mood listings, 65,438 unique tracks, 104,028 memberships.**

### Library coverage: 71.3%

Well above the 30-60% predicted earlier in this document.

| Source | Songs |
| --- | --- |
| `artist` (propagated) | 553 |
| `atlas` (playlist membership) | 480 |
| **Total labelled** | **1,033 of 1,449** |

Artist propagation is still the single largest contributor, and it needs no API key. It was added as a
hedge against the LLM layer being unavailable and turned out to be structural.

### Recommendation quality

Measured with `scripts/quality_check.py` over 8 mood/arc cases.

| | Baseline (47% crawl, raw-fit seeds) | Full data, raw-fit seeds | **Final** |
| --- | --- | --- | --- |
| Library coverage | 59.1% | 71.3% | 71.3% |
| Mean mood fit | 0.775 | 0.805 | **0.848** |
| **Cross-mood overlap** | **0.307** | 0.089 | **0.064** |
| Distinct songs / 80 slots | 44 | 61 | **63** |
| Picks carrying a mood label | 100% | 100% | 94% |

**Mean fit was a misleading headline metric.** The baseline scored a healthy 0.775 while returning 70% the
same songs for "heartbroken" and "angry" — the whole engine had only 44 distinct songs to offer across 8
moods. Cross-mood overlap is the metric that exposes that, and it is now the one to watch.

**Credit where it belongs: the data fix did most of the work.** Overlap fell 0.307 → 0.089 from the
completed crawl and the (playlist, mood) dedupe fix alone, then 0.089 → 0.064 from distinctiveness
scoring. The seed-scoring change is real but secondary — worth recording, because the seed lists made it
look like the whole story. Note the middle column is not a perfect reconstruction of the original scoring:
it disables the distinctiveness term but keeps the softened confidence weighting, which shipped together.

### What the results actually look like

Same four moods that used to return near-identical mainstream pop:

```
heartbroken   Sunn Raha Hai · Ae Dil Hai Mushkil · Tere Bina · Beete Lamhein
focus         The Sound of Silence · Vincent · Sultans Of Swing · Dream On
angry         Thunderstruck · Back In Black · Welcome To The Jungle · Rumble
party         Brown Munde · Karan Aujla · Diljit Dosanjh · Dil Luteya
```

The Punjabi and Bollywood catalogue that the coverage probe worried about is now surfacing strongly — the
gap had two halves, and only one was missing labels. The other was scoring: those songs are distinctive
rather than central, so raw fit had systematically buried them even when labelled.

### Caveats on these numbers

- **`angry` scores lowest (0.659) but its songs are among the best.** YouTube's mood taxonomy has no
  aggressive category, so there is no anchor near high-tension/low-valence and even correct picks score
  moderately. The fit metric under-reports this mood; don't tune against it naively.
- **`Workout/hold` and `angry/mirror` still share 40%.** Both are high-energy/high-tension and the library
  has almost no genuinely aggressive labelled material to separate them.
- **`heartbroken/mirror` vs `heartbroken/lift` share 50% — that is correct**, not a defect. Same mood,
  different arc, same candidate pool; the arc changes the ordering and the destination.
- **Rated fell 100% → 94%.** Distinctive seeds reach into catalogue the atlas never covered, so some picks
  now rank on signal agreement alone. That is the right trade, and the response says so.

---

## Addendum: BPM and language filtering (2026-08-19, user-requested)

Two capabilities added after the v2 build, both requested directly.

### BPM — reversing this document's own recommendation

This plan argued for demoting BPM, and the user asked for it anyway. The original objection was
narrower than it read: **tempo is a poor proxy for _mood_, which does not make it a poor proxy for
_similarity_.** As a similarity and sequencing signal it is genuinely useful, which is what it now is.

- **Source: Deezer's public API.** No key, no auth, no attribution requirement — a materially better
  option than the GetSongBPM and AcousticBrainz routes this document originally weighed. AcousticBrainz
  remains frozen; that assessment stands.
- **Half- and double-time count as close** (`tempo.relative_distance` compares against b, b/2 and b*2).
  170bpm drum-and-bass and 85bpm hip-hop share a pulse.
- **Never propagated by artist**, unlike mood. An artist's songs share a sensibility, not a tempo;
  propagating it would invent data. This asymmetry with `label.propagate_by_artist` is deliberate.
- **Coverage measured across the full library: 541 of 1,495 songs (36.2%)**, uneven in the now-familiar
  way — Rock 67%, R&B 64%, Pop 60%, Hip-Hop 47%, but Bollywood/Hindi 16% and Punjabi 6%. The misses are
  genuine: those songs resolve to the correct Deezer track and carry `bpm: 0`. **Unknown tempo never
  drops a song.**
- **One matching bug, worth recording precisely because the first correction overstated it.** 318 songs
  came back unmatched; the artist gate was rejecting real hits for messy YouTube credits
  ("Billboard Top 100 Hits", "Shankar Mahadevan | Alyssa Men"). A title-only fallback (guarded by a
  normalised-title equality check) reclassified 129 of them — but yielded only **+24 actual BPMs, 34.5%
  to 36.2%**, because those songs mostly have no tempo in Deezer either. The fix improved the accounting
  more than the coverage, and the original "the gap is real missing data" reading was closer to right
  than the correction to it.

### Language filtering

"Songs like this Punjabi track, but only English ones" needs a language label on every candidate, and
nothing in the API provides one.

- **ytmusicapi cannot read the genre pages.** `get_mood_playlists()` raises KeyError on 25 of 27 genre
  categories — those pages lead with a "Songs" carousel of track items where its parser expects
  playlists. `taxonomy.genre_page` parses the raw browse response instead, which also harvests those
  songs as genre-labelled tracks for free.
- **Weighted voting, with English deliberately near-worthless (weight 1 against 10/50/100).** YouTube
  files Punjabi and Hindi rap under "Hip-hop", so plain majority voting labelled Sidhu Moose Wala,
  Karan Aujla and AP Dhillon as English. English is now the answer when nothing language-bearing is
  evidenced, not something that can outvote real evidence.
- **Playlist names are matched loosely** — `Punjabu` is a real playlist in this library, and an exact
  `C - <genre>` match would have ignored it.
- **Retrieval expands rather than just filtering.** This is the same lesson the mood engine needed and
  it had to be learned twice: filtering a Punjabi-seeded pool for English returned 3 of 8 requested,
  because the pool did not contain more. `recommend.bridge_expand` re-seeds from the songs that passed.
  Measured: 3 results became a full 8, drawn from a pool of 79.

### Two bugs found while testing this

- **The seed came back as its own recommendation.** Excluding by videoId is not enough — YouTube carries
  the same track many times over, so a seed resolved by search returns under a different id. Observed
  live: seeding on "Kryptonite" returned Kryptonite. `signals.same_song` now matches on normalised
  title and artist.
- **No per-artist cap on the similarity path.** The mood path gets one from the sequencer; without it,
  a language filter returned four DIVINE tracks out of eight.

### Known limitations

- **`recommend_from_song` now returns a dict**, not a list — `{"songs", "notes", "filters"}`. Filters
  that silently drop results would be exactly the failure mode this project keeps guarding against, so
  the notes had to have somewhere to live. `songs_by_artist` already had the richer shape.
- **DIVINE is labelled English.** He raps largely in Hindi, but appears only under "Hip-hop" with no
  Indian-genre or library evidence to the contrary. Genre-to-language inference has a floor, and this is
  it.
- **A hard `bpm_min`/`bpm_max` range still keeps unknown-tempo songs.** Arguably they should be dropped
  when the range is explicit; keeping them preserves the non-English catalogue, and the note reports the
  count either way.
- **Tempo does not dominate ranking** (`TEMPO_WEIGHT` 0.35): a two-signal candidate at the wrong tempo
  can still outrank a one-signal candidate at exactly the right one. Use `bpm_min`/`bpm_max` when tempo
  is a requirement rather than a preference.

---

## v4 — agentic orchestration layer (proposed, not built)

Everything above — including the mood engine's four-layer fallback and the arc sequencer — is a fixed
pipeline: given inputs, a predetermined sequence of Python calls runs and returns a result. The tool
*definitions* are well-designed, but nothing in re-com decides at runtime which tools to call, in what
order, or replans when an intermediate result is bad. That's the actual gap between "built good MCP
tools" and "built an agent."

**Proposal: a thin orchestrator agent, built on the Claude Agent SDK, that sits above `re-com`,
`spotify-mcp`, and `ytmusic-mcp` and drives them the way a person would — not the way `recommend.py`
does.**

### Why this project, not a new domain

The tool surface already exists and is already well-specified (typed inputs, documented guarantees,
`notes`/`filters` fields that report degraded results instead of hiding them). That means the new project
is 100% about the agent loop and context handling, with zero time spent on API wrappers or auth — the
actual gap, isolated.

### What it should do that the current pipeline can't

- **Open-ended goals, not fixed tool calls.** "Build me a 45-minute playlist for a run that doesn't
  repeat any artist more than twice and gets more energetic toward the end" is not a single tool call —
  it requires deciding to call `recommend_for_mood` with `arc="lift"`, checking the result against the
  artist-repeat constraint itself (no existing tool enforces that), and re-querying with a narrower seed
  set if it fails.
- **Replanning on bad intermediate results**, not silent degradation. Right now a failed signal just
  drops out and a `note` reports it (correct for a fixed pipeline). An agent should be able to *notice*
  "the mood coverage note says 40% of this batch is `artist`-propagated only" and decide to raise `limit`
  or pick different seeds, the way `recommend_from_playlist_for_mood`'s bridge_expand fix (Addendum,
  above) had to be hand-coded as a special case. Generalize that instinct into the loop itself instead of
  writing a bespoke fallback for every mode that needs it.
- **State across a multi-step session.** "Now swap out the three least energetic ones" requires
  remembering what it built, what it rejected, and why — a session memory, not a single stateless tool
  call.
- **Explicit context management as the design problem, not an assumed harness feature.** A crawl-scale
  session (`get_playlist_tracks` on a 1,500-track library, `songs_by_artist` fanned out over a dozen
  artists) produces tool output far larger than a useful prompt. Claude Code's harness compacts this for
  you automatically; building it yourself — deciding what to summarize, what to drop, what to keep
  verbatim (e.g. never truncate the exclusion set, always truncate raw track-metadata dumps) — is the
  actual mechanism behind "understanding context," not just a byproduct of using a tool that already
  handles it.

### Concrete v0 scope

1. A single script (`orchestrator.py`, new directory alongside the three existing projects) using the
   Claude Agent SDK's agent loop, given the same three MCP servers as tools.
2. One test task with a checkable success condition: "45-minute run playlist, energy rising, no artist
   more than twice" — checkable in code (sum durations, check the arc, count artist repeats) without
   human judgment, so the agent's autonomous planning has a pass/fail, not a vibe check.
3. A deliberately small context budget (e.g. cap tool-result tokens fed back into the loop well below
   what a naive implementation would use) to force the summarize/trim decision to actually matter, rather
   than coasting on a context window large enough to hide the problem.
4. Log the plan the agent actually took (which tools, in what order, what it replanned after) — that log
   is the deliverable that shows the gap closing, more than the playlist itself.

### Explicitly out of scope for v0

- No new recommendation logic — reuse `re-com`'s tools as-is. This project is about the loop around them,
  not a v3 recommendation feature.
- No UI. CLI in, playlist ID + plan log out.
- No multi-user/session persistence beyond one run. Session memory matters within a run; durable
  cross-session memory is a different, later problem.
