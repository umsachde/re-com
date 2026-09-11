# re-com — Design, Decisions & Roadmap

One document. It replaces the original `PLAN.md` and `PLAN_V2.md`, which had grown into
two interleaved session logs — the reasoning that still governs the code was buried in
dated entries about work that landed months ago.

What it keeps: the decisions, the reasons they were made, the measurements that settled
them, and the mistakes that cost something. What it drops: the blow-by-blow. If a fact
here is load-bearing, the code points at the section by name.

Read `README.md` for how to use re-com. Read this for why it is shaped the way it is.

---

## 1. What this is

An MCP recommendation **engine** that returns songs you do not already have, ranked by how
many independent discovery signals agree, on any streaming backend, explainably.

Three things make it an engine rather than a wrapper around one service's algorithm:

- **Discovery is provider-neutral.** Similarity, artist adjacency and the mood corpus come
  from a music graph (Deezer) that belongs to no backend. A service can revoke its
  discovery endpoints — Spotify did — and re-com keeps working.
- **Ranking is agreement across independent sources**, not one black-box score, and every
  result says which sources surfaced it.
- **Mood decides retrieval, not filtering**, and the result is sequenced into an arc rather
  than returned as a bag of mood-matched songs.

### The guarantee

> No result is ever a song already in Liked Music **or in any playlist** — not just the one
> it was seeded from.

Everything else in this document is negotiable. That is not. It is why exclusion is applied
last and always, why the stores are scoped per backend, why the graph path needed its own
title/artist exclusion index, and why `refresh_library()` exists.

### Hard requirements

1. Never recommend a song already in the library (above).
2. Never write to the user's account. re-com is read-only; see §4.9.
3. Degrade with a stated reason, never silently. A missing signal, an unresolvable
   candidate, a shortfall against `limit`, an uncovered mood — each is reported in `notes`,
   `filters`, `match_quality` or `seed_report`, not smoothed over.
4. Hold no streaming-service credentials. Auth lives entirely in the sibling `*-mcp`
   servers.

---

## 2. Architecture

Four layers, each with one job.

```
  tools            server.py                 the MCP surface, arg validation, error translation
    |
  engine           recommend.py  signals.py  seeding, candidate gathering, ranking, arcs
                   filters.py    match.py    language/tempo filters, song identity
    |
  knowledge        store.py      label.py    mood labels, feedback, history, library cache
                   graph*.py     moodspace.py  the neutral music graph, the mood vector space
    |
  provider seam    provider.py               capabilities + backend selection
                   ytmusic_client.py  spotify_client.py
```

### 2.1 The provider seam

`provider.Provider` is not a new abstraction — it is `ytmusicapi.YTMusic`'s method
signatures and return shapes, written down. v1 was built against that surface directly, and
formalising the shape it already had meant a second backend needed **zero changes** to
`signals.py`, `recommend.py` or their tests. `spotify_client.py` translates Spotify's API
into those shapes; a third backend does the same.

Each backend declares `capabilities()` — which native discovery signals it can actually
supply — rather than being probed. Capability is a property of the app registration, not of
a request, so probing would spend a round-trip learning something knowable in advance.

**Declaration decides what to attempt; exceptions still decide what survives.** A
declaration can go stale, so per-signal exception handling stays underneath. Both, not
either.

### 2.2 Candidate generation and ranking

For each seed, candidates are pooled from every available signal:

| Signal | Source | Available on |
| --- | --- | --- |
| `radio` | the provider's per-track radio/autoplay queue | native only |
| `related` | the provider's per-track related-content feed | native only |
| `artist` | the provider's artist catalogue + related artists | native only |
| `graph_artist` | the seed artist's Deezer catalogue | every backend |
| `graph_radio` | Deezer artist radio | every backend |
| `graph_related` | adjacent artists' catalogues on Deezer | every backend |

**A candidate's score is how many distinct (seed, signal) pairs surfaced it.** Not a
weighted blend — a count of independent agreement. A backend with fewer native signals has
fewer sources agreeing, rather than returning nothing.

Exclusion is applied **last, always**, after ranking and after any filter.

### 2.3 The music graph

Similarity, artist adjacency, tempo and the cross-backend mood corpus come from
[Deezer](https://developers.deezer.com/api) — no key, no auth, no attribution. Cached in
`~/.recom/graph.db`.

**Known costs, stated up front:**

- Deezer has no track-level radio (`/track/{id}/radio` does not exist), so graph similarity
  is **artist-centric** — genuinely weaker than a per-track radio. This is why native
  signals are *added to* rather than replaced.
- The graph returns *"Diljit Dosanjh — Born to Shine"*, not an id your backend understands,
  so candidates are matched back by search. That resolution is **lazy**: ranking happens on
  graph metadata, and only the top of the pool is ever resolved, so a fully-native response
  performs zero extra lookups. A candidate that cannot be matched is dropped with a note,
  never substituted.
- Negative results are cached alongside positive ones, so a song Deezer genuinely does not
  carry costs two searches once rather than forever.

### 2.4 The mood engine

`recommend_from_song` answers *"what sounds like this?"*. `recommend_for_mood` answers
*"what does this person need to hear right now?"* — a different question with a different
retrieval strategy (§4.4).

**The mood vector**, four axes:

| Axis | Range | Low ←→ high |
| --- | --- | --- |
| `valence` | −1…1 | despairing ←→ euphoric |
| `energy` | 0…1 | still ←→ frantic |
| `tension` | 0…1 | resolved ←→ anxious |
| `depth` | 0…1 | background ←→ lyric-forward |

`tension` is the axis that earns its place: two axes cannot separate aggressive workout rap
from joyful party pop. Both are high-energy and nominally positive; only tension tells them
apart.

**How a song's mood is known** — five layers, best available source wins outright
(`label.SOURCE_PRIORITY`):

| Layer | What it is | Cost |
| --- | --- | --- |
| `llm` | Claude reads the lyrics. Handles any language, and irony. | API key, optional |
| `lyrics` | Lyrics fetched and cached (2 calls/song, incl. the negative result) | free |
| `atlas` | Membership in the service's own editorial mood playlists | a crawl, YouTube only |
| `graph_atlas` | Membership in Deezer playlists found by mood search | a crawl, any backend |
| `artist` | An artist's average mood, propagated to their unlabelled songs | free |

`graph_atlas` ranks *below* `atlas` deliberately: a playlist merely titled "sad songs" was
named by a stranger, where an editorial mood playlist was filed by the service under a
taxonomy. It ranks *above* `artist` because real membership beats inference.

**Arcs.** A mood-matched set is the obvious thing to return and the wrong one. From music
therapy's iso-principle: to shift someone's mood you meet them where they are and move
gradually. Opening with upbeat songs when someone is low just gets skipped.

| Arc | Behaviour |
| --- | --- |
| `mirror` | Stay where they are and validate it. Default. |
| `lift` | Start at their mood, rise gradually across the set. |
| `settle` | Descend to calm — an evening wind-down. |
| `deepen` | Go further in. |
| `hold` | Stay in a band with energy as a curve (warmup → peak → cooldown). |

---

## 3. Measured ground truth

Every number here was measured against the real account, not estimated. Dates matter
because several of them are the reason a decision went the way it did.

### Coverage

| | Measured |
| --- | --- |
| YouTube library with a mood label (full crawl) | **71.3%** — 553 from artist propagation, 480 from playlist membership |
| YouTube editorial atlas alone, 60-playlist sample | **4.1%** of liked songs |
| Spotify library resolved to Deezer | **324 / 333 (97.3%)** |
| Spotify library with a `graph_atlas` mood | **104 / 333 (31.2%)** |
| Spotify library with any mood | **134 / 333 (40.2%)** |
| Library with a BPM | **541 / 1,495 (36.2%)** |

BPM coverage by genre, because the gaps are not random:

| | | | |
| --- | --- | --- | --- |
| Rock & Alternative | 67% | Hip-Hop & Rap | 47% |
| R&B & Soul | 64% | Electronic & Dance | 38% |
| Pop | 60% | **Bollywood/Hindi** | **16%** |
| Country | 56% | **Punjabi** | **6%** |
| Reggae & Dancehall | 49% | | |

The misses are genuine: those songs resolve to the correct Deezer track and carry `bpm: 0`.
So **a song with unknown BPM is never dropped**, only left unscored — dropping them would
quietly delete whole languages from the results.

### Quality

Measured by `scripts/quality_check.py` over a fixed set of mood/arc cases.

| | YouTube, no graph | YouTube + graph | Spotify + graph |
| --- | --- | --- | --- |
| mean mood fit | 0.797 | **0.820** | 0.767 |
| cross-mood overlap (lower better) | 0.121 | **0.096** | 0.201 |
| distinct songs / slots | 58 / 80 | 58 / 80 | 43 / 74 |
| rated | 98% | 98% | 85% |

**Watch cross-mood overlap, not mean fit.** An early build scored a healthy 0.775 mean fit
while returning 70% the same songs for "heartbroken" and "angry". Fit alone could not see
it. This is the single most useful measurement lesson in the project.

### Speed

| | Before | After |
| --- | --- | --- |
| Building the exclusion set | 20.5s | **0.9s** (disk cache) |
| Seed gathering, six seeds | 18.9s | **3.1s** (concurrent) |
| `recommend_from_song` end to end | ~24s | **4.3s** |
| `recommend_for_mood` end to end | ~18s | **5.7s** |
| `songs_by_artist` end to end | ~22s | **2.6s** |

**Results are not identical run to run, and were not before concurrency either.** Measured:
two *serial* runs of the same seeds overlap 0.793; serial vs. concurrent overlaps 0.819.
Concurrency is not what varies the output — the API is.

### What Spotify actually revoked (2026-08-23)

For apps registered after November 2024 without Extended Quota Mode:

| Still works | Returns 403/404 |
| --- | --- |
| Saved tracks, playlists, recently played, top tracks/artists | `/recommendations` (404) |
| `search` (tracks, artists, playlists) | `artist_related_artists`, `artist_top_tracks` |
| `track`, `artist`, `artist_albums` → `album_tracks` | `audio_features`, `audio_analysis` |
| | reading **any other user's playlist** |
| | `categories`, `featured_playlists`, `new_releases` |

Two of three native discovery signals are unbuildable there. Before the graph,
`recommend_from_song` returned **zero songs** on Spotify. After: a full result set (10
songs, 3.4s warm).

---

## 4. Decisions, and what they were made against

Each of these had a cheaper alternative that was rejected for a stated reason.

### 4.1 Multi-signal agreement, not one algorithm

*Rejected: call the service's radio endpoint and return it.* That is the thing re-com is
supposed to beat. One black-box algorithm has one set of blind spots and no way to express
confidence. Counting independent agreement gives both a ranking and an explanation
(`sources`), and it degrades gracefully as signals disappear.

### 4.2 Exclusion applied last, always

*Rejected: filter during candidate generation, to save work.* Filtering early means every
new code path has to remember to filter, and the one that forgets fails silently. Applying
it once, last, makes the guarantee structural. This was already learned the hard way: v1
filtered against Liked Music only, so a song sitting in some other playlist came back as
"new" — fixed by giving all tools one exclusion definition.

The graph path needed this lesson re-applied. A graph candidate is keyed
`graph:<deezer id>` and carries no provider id, so testing it against a set of provider ids
always passes. Without a title/artist exclusion index, the guarantee silently stopped
applying to every graph candidate on the mood path.

### 4.3 A neutral music graph, not a second provider integration

*Rejected: accept that Spotify has fewer signals.* Graceful degradation of *every* signal
is not graceful, it is zero. The graph moves similarity off the backend entirely, so what
the provider supplies is only *whose taste this is* — library, history, playlist writes.
That is the part no third party can provide and the part no service is likely to revoke.

### 4.4 Mood decides retrieval, not filtering

*Rejected: run the v1 engine and filter its results by mood.* Measured and abandoned:
filter a Daft Punk radio for "melancholy" and you get the least danceable Daft-Punk-adjacent
tracks, not melancholy music. So the mood picks the seeds — library songs already near the
target vector — and the proven v1 signals run from there.

### 4.5 Five mood layers, best-source-wins

*Rejected: one source, or a weighted blend of all of them.* The editorial atlas alone
covers 4.1% of this library and its misses concentrate on the Punjabi, Bollywood and Reggae
catalogue that English-centric mood playlists barely touch. Artist propagation closes most
of the gap for free; the LLM layer closes the rest. Blending would let a weak source dilute
a strong one — a lyric reading is not improved by averaging it with an artist's mean mood.

### 4.6 Filler is capped at 25%

`limit` is a ceiling, not a promise. The arc sequencer will fill every requested slot from
whatever is left if allowed to — asking for 100 with 7 genuine matches returned 100, the
other 93 progressively worse guesses (an unrated song still gets a placeholder fit score
and can still win a slot). Filler is now capped at 25% of `limit`, genuine matches are
never capped, and `match_quality` reports the split. That request now returns 32, not 100.

### 4.7 BPM — reversing this document's own recommendation

The original plan refused BPM: YouTube Music exposes no tempo, and inventing it was worse
than omitting it. Deezer's public API changed the facts, so it was built. Two rules kept
from the refusal:

- **Half- and double-time count as close.** 170bpm drum-and-bass and 85bpm hip-hop share a
  pulse; treating them as opposites is musically wrong.
- **Tempo is never propagated by artist**, unlike mood. An artist's songs share a
  sensibility, not a BPM. Propagating it would be inventing data.

### 4.8 English is weighted at 1 in language inference

YouTube files Punjabi and Hindi rap under "Hip-hop", so counting an English-genre hit as a
normal vote labelled Sidhu Moose Wala, Karan Aujla and AP Dhillon as English. English is now
what you get when *no* language-bearing evidence exists, rather than something that can
outvote real evidence (script: 100, library playlist names: 50, genre: 10, English genre: 1).

Two consequences worth knowing: unlabelled candidates are dropped by default when a language
is requested (asking for English only is a request for a guarantee), and filtering alone is
not enough — seeding from a Punjabi song and filtering for English left 3 results out of 8,
so surviving songs are re-seeded to reach further into that language.

### 4.9 Read-only, deliberately

*Rejected: let re-com create the playlist it just recommended.* A recommender that also
mutates the library cannot be trusted to have excluded what it just added. So the flow is
`recommend_*` → a playlist-management tool → `refresh_library()`. The third step is not
optional: without it the cached exclusion set is stale for up to `RECOM_CACHE_TTL` and a
later call can recommend a song you just saved.

This is the one decision with a known ergonomic cost; see §7.5.

### 4.10 One store per backend, one graph cache for all

Every id re-com persists belongs to exactly one backend's namespace, and they are not
interchangeable: a YouTube videoId is 11 characters, a Spotify track id is 22. So each
provider instance gets its own store and exclusion cache.

**This is a correctness guarantee, not tidiness.** Sharing one exclusion set between
backends does not merely mix the data — it voids the promise the project exists for,
because no videoId can ever equal a Spotify track id, so a 1,499-entry exclusion set matches
*nothing* and every "new" recommendation could already be in the library.

The graph cache is the deliberate exception: *"Excuses — AP Dhillon is Deezer track
1508646682"* is equally true on every backend. Scoping it per provider would resolve every
artist twice and grow a third copy on the next service.

### 4.11 Concurrency is capped, not unbounded

Six seeds × ~4 round-trips is fine; a playlist-seeded mood request can carry 20 seeds, and
20 × 4 simultaneous in-flight requests is exactly the rate-limit exposure worth avoiding.
`RECOM_SEED_WORKERS` (default 6) bounds it.

### 4.12 Learning without being asked

`record_feedback` only fires when someone remembers to call it, which in practice is almost
never. Two tables already kept — `recommendation` (what was served) and `history_log` (what
was played) — yield two signals for free by diffing:

| Inferred | When | Strength |
| --- | --- | --- |
| `played` | the song appeared in history after being recommended | strong |
| `ignored` | it did not, and ≥3 history snapshots have been taken since | weak |

The threshold matters: below it, "not played" almost always means "the cron has not run
yet". Songs under it are reported as `pending` and nothing is inferred.

**Inferred evidence never hard-excludes.** A stated `skipped`/`wrong_mood` bans a song
permanently; `ignored` only demotes, because absence from a history log has too many
innocent explanations. What is learned applies **per artist**, not per song — a song that
got played usually gets liked, at which point library exclusion means it can never be
recommended again anyway; what survives is the direction it pointed in. The multiplier is
bounded to 0.75–1.25 and saturates at 3 net reactions, so a learned preference breaks ties
without overruling signal agreement, and any nudge is reported in `affinity`.

---

## 5. Verification

Three layers, each responsible for something the others structurally cannot check. The
split exists because a real defect shipped through the gap between them (§6.1).

| Layer | Runs | Covers | Cannot cover |
| --- | --- | --- | --- |
| `pytest` (unit) | CI, every push/PR | pure logic against fakes — normalization, scoring, ranking, exclusion, cache behaviour, arcs, label resolution, error translation, every tool end to end | anything requiring a real connection, a real thread, or a real account |
| `scripts/smoke_all.py` (live) | by hand, before a release | every tool × every configured backend against the real account | nothing runs it automatically; it needs credentials |
| `scripts/quality_check.py` | by hand, when ranking changes | mood fit, cross-mood overlap, distinctiveness | whether the tools return at all |

### The unit suite

No network, no credentials, no `*-mcp` server. `conftest.py` redirects the library cache,
the store and the graph cache to temp paths for every test, and a `no_network` fixture fails
any test that opens a real socket — added after v6 silently made six "no network" tests call
api.deezer.com for real. They still passed, which is the point.

`tests/test_graph_concurrency.py` is the exception that proves where fakes stop: it uses a
**real** sqlite connection crossing a **real** thread pool, because that is the one shape a
fake cannot model and the shape that shipped broken. Its tests were verified by removing the
fix and confirming 7 of 9 fail.

### The live smoke harness

`scripts/smoke_all.py` runs every tool against every configured backend, each in its own
subprocess (`RECOM_PROVIDER` is read once at import, so one process cannot honestly test
two). Three invariants per tool:

- **returns** — a non-empty result, or an explicit stated reason. Silent emptiness is the
  failure mode being hunted.
- **excludes** — nothing already in the library. This is the guarantee, and a live run is
  the only place it can actually be tested.
- **within** — a latency ceiling. A tool that quietly starts taking 60s has regressed even
  if the songs are right.

The playlist tools are seeded from a playlist with **2+ tracks** on purpose: a single seed
stays on the calling thread and exercises none of the threading. A backend with no command
configured is reported as `skipped`, never as a pass — an unrun check must not read as a
green one. `record_feedback` is behind `--include-writes` because it writes to the real
store.

`tests/test_smoke_harness.py` unit-tests the harness's own judgement, because a smoke test
that cannot fail is worse than no smoke test: it reads as evidence.

---

## 6. Lessons that cost something

Kept because each one was paid for, and each generalises.

### 6.1 A forgiving path and a strict path over the same code hide each other's bugs

`sqlite3.threadsafety` is 1: a connection used off its creating thread raises. `gather_seeds`
fans seeds across a thread pool and handed each worker the graph connection, so every
multi-seed request with the graph enabled hit that — and what it cost depended entirely on
the caller. `recommend_from_playlist` passes `skip_failures=False`, so it did not degrade,
it **failed outright, on both backends, for any multi-track playlist, from the v6 merge
until 2026-08-29**. The mood path passes `skip_failures=True`, so the same defect would have
silently emptied its candidate pool. `recommend_from_song` shows neither symptom, because a
single seed deliberately stays on the calling thread — and it was the only tool the v6 smoke
tests exercised.

One path turns a defect into missing results, the other into a crash. Testing only the
forgiving one leaves the strict one broken in production. Fixed with `graph_store.for_thread`
(a per-thread connection) rather than `check_same_thread=False` — at threadsafety 1 that flag
disables the check without making concurrent use safe. §5 is the structural answer.

### 6.2 A coverage number is a precondition for a gate, not a substitute for measuring what the gate protects

Spotify mood coverage measured at 40.2%, above the bar the gate had set. Had the gate been
opened on that number, it would have shipped returning **0 songs on all 8 quality-check
cases, in 0.0s each** — because `recommend.build` called `gather_seeds` without `graph_conn`,
so a backend with no native capabilities gathered nothing at all. The index was ready; the
pipeline was not, and only measuring the pipeline could tell.

### 6.3 The same bug three times means the abstraction is missing

A joined credit string read one character at a time, three separate times: `store._artist_names`
("AP Dhillon" → `"A & P & ..."`), `taxonomy.as_languages` (`language="english"` → a filter
for `e, n, g, l, i, s, h`), and the graph bridge looking up an artist called `"A"`. Three
instances is enough — normalising now lives once, in `match.artist_list`, next to the rest
of song identity.

### 6.4 Subclass before superclass

`handle_errors` caught the general server error before the gated-content error, and since the
latter subclasses the former, the clearer branch was dead code. Found by writing a test for
it, not by reading it.

### 6.5 Resolution failures must re-sequence, not append

On the mood path, resolution happens *after* sequencing — the sequencer has already chosen
the songs, so that is the smallest set needing provider ids. But a candidate that fails to
resolve leaves its arc slot empty, and appending a replacement puts an unslotted song at the
end of a curve the user asked to be shaped. Measured: a 10-song request came back with 9.
Dead candidates are dropped and the arc re-sequenced, up to `_RESOLVE_ROUNDS`.

---

## 7. Roadmap

Ranked by what the project actually needs, not by size. Item 1 is in progress; the rest are
open.

### 7.1 Verification across tools × backends — *in progress*

The gap §6.1 shipped through. Three parts:

- [x] `tests/test_graph_concurrency.py` — a real graph connection across a real thread pool,
      asserting both caller shapes. Verified to fail when the fix is removed.
- [x] `scripts/smoke_all.py` — every tool × every backend, live, with stated invariants.
- [x] `tests/test_smoke_harness.py` — the harness's own judgement, so it cannot rot into
      always-passing.
- [x] `.github/workflows/tests.yml` — CI on 3.10 / 3.12 / 3.13.
- [ ] Run `smoke_all.py` against both live backends and record the baseline numbers here.

### 7.2 A quality number for the similarity path

`quality_check.py` scores the mood path and the graph. `recommend_from_song` and
`recommend_from_playlist` — the most-used tools — are judged by impression. §3's own lesson
(fit alone could not see 70% cross-mood duplication) says that is not good enough.

Proposed: fixed seed cases measuring signal-agreement distribution, artist concentration,
cross-seed overlap, and a native-vs-graph-only A/B. Without it there is no way to tell
whether graph candidates are helping or diluting.

### 7.3 A second music graph source

Deezer is now a single point of failure, and §4.3's argument condemns exactly that: building
discovery on one service's endpoints is how you get 403'd into nothing. Deezer is keyless,
unauthenticated, contract-free and under no obligation to anyone.

Proposed: a second source behind the same `graph.py` seam — MusicBrainz/ListenBrainz is the
obvious pick (open data, real similarity, no key) — so "the graph" becomes plural the way
"signals" already is. This is the most on-brand item on the list.

### 7.4 Continuous indexing

Coverage is the quality ceiling (§3) and it is crawl-bound: the measurement says growing it
means crawling more of Deezer, not resolving better. Yet every crawl is a manual one-shot
script, so an install's index is as good as the last time someone remembered.

Proposed: one `scripts/maintain.py` on cron that tops up the graph atlas, tempo and labels
for anything new since the last run, and an `index_status()` that reports staleness and
trend rather than only totals. It turns a 45-minute setup burden into a self-maintaining
index.

### 7.5 Close the read-only handoff gap

§4.9's decision is right; the ergonomics around it are a footgun. Turning a recommendation
into a playlist is three steps and skipping the third leaves the exclusion set stale for up
to six hours.

Proposed, without letting re-com write anything:

- `refresh_library(video_ids=[...])` unions specific ids into the cache instantly, with no
  ~20s rebuild — so the agent that just added tracks can close the loop cheaply.
- A `served` staging set, so a song handed out 30 seconds ago cannot come back in the next
  call before it has been saved anywhere. "No repeats within a session" is the failure users
  actually notice.

### 7.6 Respect native dislikes

Never recommend a song thumbs-downed on the service, the way Liked Music is already excluded.

**There is no bulk API for this.** `ytmusicapi` has `get_liked_songs()` but no
`get_disliked_songs()`. A song's `likeStatus` is exposed only per-song or inside
`get_history()`'s most recent 200 items. So this has to be a **persistent log, not a
snapshot fetch**: extend `scripts/snapshot_history.py` (already on a cron) to read
`likeStatus` off each history item and upsert into `feedback` with `source="native_dislike"`,
reusing the existing `rejected_video_ids()` exclusion machinery rather than inventing a
second filter path.

**Coverage will be partial and grows only over time** — a song disliked once and never seen
again in a 200-item window is never observed. That must be stated plainly in the tool docs
rather than implying a Liked-Music-grade guarantee.

### 7.7 An agentic orchestration layer

Everything above is a fixed pipeline: given inputs, a predetermined sequence of calls runs.
The tool *definitions* are well-designed, but nothing in re-com decides at runtime which
tools to call, in what order, or replans when an intermediate result is bad.

Proposed: a thin orchestrator on the Claude Agent SDK above `re-com`, `spotify-mcp` and
`ytmusic-mcp`. What it should do that the pipeline cannot:

- **Open-ended goals.** "A 45-minute run playlist, no artist more than twice, getting more
  energetic toward the end" is not one tool call — it needs `arc="lift"`, a constraint check
  no tool enforces, and a re-query with narrower seeds if it fails.
- **Replan on bad intermediate results**, rather than the pipeline's correct-but-passive
  "note that 40% of this batch is artist-propagated". Generalise that instinct into the loop
  instead of hand-coding a bespoke fallback per mode.
- **State across a multi-step session.** "Now swap out the three least energetic ones"
  requires remembering what it built and why.
- **Explicit context management as the design problem.** A crawl-scale session produces tool
  output far larger than a useful prompt; deciding what to summarise, drop, or keep verbatim
  (never truncate the exclusion set; always truncate raw metadata dumps) is the actual
  mechanism, not a harness freebie.

v0 scope: one script, one test task with a **checkable** success condition (sum durations,
check the arc, count artist repeats — pass/fail, not a vibe check), a deliberately small
context budget so the trim decision matters, and a log of the plan the agent actually took.
That log is the deliverable.

**Sequence it after 7.1–7.3.** An agent that replans on bad intermediate results is only as
good as the tools' honesty about being bad. Build it first and it replans on vibes.

### 7.8 Movies & TV — a sibling project, not a feature

Recommending films the way re-com recommends songs is a bigger fork than adding Spotify was.
v3 worked because `signals.py` and `recommend.py` needed **zero changes** — only the client
and its shape translation. Movies have no sibling `*-mcp` server playing ytmusic-mcp's role
(owns auth, exposes watched history, exposes catalogue + related signals); that server would
have to be built first.

This is closer to standing up `re-com-movies` — its own `trakt-mcp`/`tmdb-mcp`, its own
`Provider`, its own signal design — than extending this one. Reasonable first slice: the
sibling auth server and a single `recommend_from_title` against TMDb's `/similar` +
`/recommendations` alone, with **no** watched-history exclusion yet, documented as a known
gap, before deciding whether Trakt is worth a second OAuth integration just for that.

### 7.9 Smaller items

- `server.py` is ~1,100 lines with tool bodies doing filter + store + resolve orchestration
  inline. Worth extracting a `tools/` layer.
- `ytmusicapi` is a hard top-level dependency that the live path never imports — only the
  offline scripts use it. It belongs in an extra; a Spotify-only install currently pulls in
  a YouTube Music client it never calls.
- The offline maintenance scripts still authenticate directly with their own
  `headers_auth.json` rather than going through `ytmusic-mcp`. Deliberate — they are bulk
  indexing jobs, not part of the live path — but it means two unrelated credentials exist.

---

## 8. Version history

Condensed. Each entry is what changed and why; the reasoning lives in §4 and §6.

**v1 — the engine.** Multi-signal candidate generation (radio / related / artist expansion),
agreement-based ranking, library-wide exclusion, `recommend_from_song`,
`recommend_from_playlist`, `songs_by_artist`, seed-by-search. Fixed the exclusion bug where
only Liked Music was filtered, and added the disk-backed library cache (20.5s → 0.9s).

**v2 — mood.** The four-axis vector space, the layered label system, arcs, mood sensing from
history, `recommend_for_mood`, `read_my_mood`, `explain_recommendation`, `record_feedback`,
`index_status`. Later: `recommend_from_playlist_for_mood`, the 25% filler cap, implicit
feedback from the recommendation/history diff, bounded artist affinity, and concurrent seed
gathering (18.9s → 3.1s).

**Addendum — BPM and language.** Reversed v2's refusal of BPM once Deezer made it possible,
and added layered language inference with English weighted at 1.

**v3 — multi-provider.** re-com stopped holding any streaming credential; both backends
moved behind sibling `*-mcp` subprocesses and the `provider.Provider` seam. Spotify added.
Per-backend store and cache scoping followed, as a correctness guarantee.

**v6 — the neutral graph.** Discovery moved onto Deezer after measuring what Spotify had
revoked: `recommend_from_song` went from 0 songs to a full result set there, and YouTube's
top ten was unchanged. The graph atlas gave every backend a mood corpus.

**2026-08-29 — opening the mood gate.** Wired in the order that mattered: graph threaded
through `recommend.build`, measured on both backends, and only then `"spotify"` added to
`MOOD_PROVIDERS`. YouTube improved on both headline metrics rather than regressing. Found
and fixed the thread-safety defect (§6.1) and the third instance of the joined-credit bug
(§6.3) on the way.

**2026-09-10 — verification.** §5's three-layer split, after auditing why §6.1 could ship:
the real-connection concurrency tests, the live cross-backend smoke harness, tests for the
harness itself, and CI.
