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

### 4.11 "Your playlists" means playlists you own

*Rejected: treat everything `get_library_playlists` returns as the user's own.* Spotify's
`current_user_playlists` returns playlists the user follows alongside ones they created,
distinguishable only by `owner.id` — measured on a real account, 4 of 8 library playlists
belonged to other users. Reading a followed playlist's tracks 403s (it isn't yours to read
that way), and the exclusion builder was catching that failure and silently skipping the
playlist, which meant those tracks were never excluded while `refresh_library()` reported a
confident total.

Filtering to owned playlists is the fix, not catching the 403 more gracefully: a playlist
you follow is not one of "your playlists" in any sense you would recognise — you did not put
those songs there — so it should not be read for exclusion or seeded from at all. YouTube
Music needs no equivalent filter; `get_library_playlists` there already returns only the
user's own.

### 4.12 Concurrency is capped, not unbounded

Six seeds × ~4 round-trips is fine; a playlist-seeded mood request can carry 20 seeds, and
20 × 4 simultaneous in-flight requests is exactly the rate-limit exposure worth avoiding.
`RECOM_SEED_WORKERS` (default 6) bounds it.

### 4.13 Learning without being asked

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
| `scripts/quality_check.py` | by hand, when ranking changes | mood fit, cross-mood overlap, distinctiveness; with `--similarity`, signal agreement against its ceiling, artist concentration, cross-seed overlap, native-vs-graph displacement, and a measured noise floor | whether the tools return at all |

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

### 6.5 The first live run of a test is part of writing it

`smoke_all.py`'s first real run reported 4 failures on Spotify. Two were genuine and
serious (§6.6). The other two were the harness being wrong: it marked `read_my_mood`
FAILED for correctly saying it had no labelled plays to read a mood from, and
`recommend_from_playlist_for_mood` FAILED for correctly refusing to seed from tracks that
did not fit. Both are behaviours the README promises; the harness had encoded "returned
something" as the contract instead of "returned something *or said why not*".

It also picked an empty playlist to seed from, because Spotify reports no track count and
the harness trusted it, then reported the resulting correct error as a regression.

A harness that cries wolf gets ignored exactly when it is right, so these are not cosmetic.
Each is now a unit test in `tests/test_smoke_harness.py`.

### 6.6 A shape assumption held for one payload and not the other

Two defects found by the first live cross-backend run, both silent, both in `spotify-mcp`.

**Every Spotify playlist read returned zero tracks.** Spotify returns two payloads for a
playlist row: the documented one puts the track object under `track`; the one this account
receives puts it under `item` and uses `track` as a *boolean* flag meaning "this is a track,
not an episode". Reading `it["track"]` yielded a bool or None for every row, so the filter
dropped all of them — a playlist reporting `total=20` returned 0 tracks.

That voided the guarantee this project exists for, on one backend, invisibly: the exclusion
set is built from those rows, so it covered saved tracks only and every song in every
Spotify playlist could be handed back as "new". Measured after the fix: the exclusion set
went from 328 to **454** tracks, and `recommend_from_playlist` went from unusable to working.

**`songs_by_artist` returned nothing, and said nothing.** `artist_top_tracks` 403s on a
restricted registration, leaving an empty catalogue and a result of `found: 0` with no note
— which reads as "your library already has them all". The album walk (`artist_albums` →
`album_tracks`) is the route that survives, and it needed its own measurement: that endpoint
rejects any page size above 10 on this registration ("400 Invalid limit" at 20, 49 and 50),
while `album_tracks` and `current_user_playlists` accept 50 on the same app. The cap is
per-endpoint, so only the one that has it pays for smaller pages.

Both had been shipping since Spotify support was added. Neither is visible from unit tests,
because a fake returns the shape the fake's author expected.

### 6.7 Resolution failures must re-sequence, not append

On the mood path, resolution happens *after* sequencing — the sequencer has already chosen
the songs, so that is the smallest set needing provider ids. But a candidate that fails to
resolve leaves its arc slot empty, and appending a replacement puts an unslotted song at the
end of a curve the user asked to be shaped. Measured: a 10-song request came back with 9.
Dead candidates are dropped and the arc re-sequenced, up to `_RESOLVE_ROUNDS`.

---

## 7. Roadmap

Ranked by what the project actually needs, not by size, as of when each item was written;
numbered in the order items were added. Open: 7.6, 7.9. Everything else is done
or closed with its result recorded. (7.8, Movies & TV, moved out to a sibling project —
`re-com-movies` — and is tracked there, not here.)

### 7.1 Verification across tools × backends — *done*

The gap §6.1 shipped through. Three parts:

- [x] `tests/test_graph_concurrency.py` — a real graph connection across a real thread pool,
      asserting both caller shapes. Verified to fail when the fix is removed.
- [x] `scripts/smoke_all.py` — every tool × every backend, live, with stated invariants.
- [x] `tests/test_smoke_harness.py` — the harness's own judgement, so it cannot rot into
      always-passing.
- [x] `.github/workflows/tests.yml` — CI on 3.10 / 3.12 / 3.13.
- [x] `tests/test_packaging.py` — the flat-layout module list, enforced in both
      directions.
- [x] Run `smoke_all.py` against both live backends and record the baseline (below).

**Baseline, 2026-09-10.** Warm, `limit=10`, graph on. Every tool passes on both backends.

| | YouTube | Spotify |
| --- | --- | --- |
| exclusion set | 1,963 tracks / 18.0s | 454 tracks / 4.6s |
| `recommend_from_song` | 5.2s | 3.3s |
| `recommend_from_playlist` | 15.1s | 6.3s |
| `songs_by_artist` | 2.9s | 7.1s |
| `recommend_for_mood` | 8.4s | 4.0s |
| `recommend_from_playlist_for_mood` | 26.7s | refused, explained |
| `read_my_mood` | 2.2s | no mood, explained |

Two things this corrects in the older numbers above. §3's "~4-6s warm" held only
for `recommend_from_song`; the playlist-seeded mood path is **26.7s** on YouTube, four
times what the docs implied. And the exclusion-set rebuild is 18s, not the 0.9s quoted
there — that figure is a *cache hit*, which is the common path but not what
`refresh_library()` pays.

**CI found a real defect on its first run, and not the kind that was being looked for.**
`pip install -e .` — the README's own setup step — failed outright: setuptools refuses
auto-discovery on a flat layout with 21 root modules, so the project had been uninstallable
on any recent setuptools. Nobody saw it because *existing* installs were unaffected and the
repo's own `.venv` predated the enforcement. Same shape as §6.1: the working path hid the
broken one. Fixed by declaring `py-modules` explicitly, with `tests/test_packaging.py`
keeping the list honest — an undeclared new module imports fine from a checkout and is
simply missing from an install, which is the quiet half of that failure.

### 7.2 A quality number for the similarity path — *done*

`quality_check.py` scored the mood path and the graph. `recommend_from_song` and
`recommend_from_playlist` — the most-used tools — were judged by impression. §3's own lesson
(fit alone could not see 70% cross-mood duplication) says that is not good enough.

Built as `quality_check.py --similarity`, over the seed lists §3 already splits by
catalogue, plus one multi-seed playlist case:

- [x] **Signal agreement**, reported against its ceiling. A score counts distinct
      (seed, source) pairs, so the ceiling is 6 per seed on YouTube and 3 on Spotify
      (`capabilities()` is empty there, §3) — 30 vs 15 for a five-seed playlist case. A bare
      mean across backends would have reported arithmetic as a regression.
- [x] **Artist concentration** (HHI + largest share), measured *before* `max_per_artist`.
      After the cap it is pinned at 2/`limit` and only confirms the cap works.
- [x] **Cross-seed overlap** — the direct analogue of cross-mood overlap, and the one to
      watch for the same reason: it catches every seed funnelling into one popular attractor.
- [x] **Native-vs-graph A/B** reporting **churn** and the **corroboration delta**. This
      started as "how many native picks did the graph displace", which the first live run
      killed: both arms truncate to `limit`, so whenever both fill up, displaced == added
      identically — 37 == 37 across ten YouTube cases. Arithmetic wearing a finding's
      clothes, and §6.5's lesson landing again. Churn is that number named honestly; the
      delta is what actually answers helping-or-diluting.
- [x] `--repeat`, which measures the noise floor **in the same run**. §3 measured two
      identical serial runs overlapping 0.793, so a delta under ~20% is not a result. Left
      to memory, that fact is exactly §6.2's mistake waiting to happen.
- [x] `tests/test_quality_metrics.py` — the metric's own arithmetic, since like the smoke
      harness this cannot run in CI. Verified by mutation: three deliberate breaks
      (agreement threshold, graph crediting, the ceiling) each fail a test.
- [x] **Run it against both live backends and record the baseline** (below).

**Baseline, 2026-09-10.** Warm, `limit=10`, ten cases (nine single-seed, one five-seed
playlist), 100 slots per backend.

| | YouTube | Spotify |
| --- | --- | --- |
| agreement ceiling | 6 / seed | 3 / seed |
| corroborated (picks >1 pair agreed on) | **0.47** | **0.13** |
| artist concentration (HHI, lower better) | 0.224 | 0.272 |
| cross-seed overlap (lower better) | 0.042 | 0.044 |
| distinct songs / slots | 82 / 100 | 80 / 100 |
| graph churn | 35 / 100 | 100 / 100 |
| corroboration delta (graph on vs off) | **+0.09** | n/a — no native arm exists |
| **noise floor (identical runs overlap)** | **0.87** | **1.00** |

Four things this says that were not known before.

**The noise floor is not a property of the project, it is a property of the backend.** §3
recorded 0.793 and it has been quoted since as though it were universal. YouTube measures
0.87 here; Spotify measures **1.00** — bit-identical results across repeated runs, because
every candidate comes from the locally cached graph and nothing upstream varies. So a 5%
A/B delta is noise on YouTube and a real result on Spotify. Any future comparison has to
say which backend it was measured on.

**The graph helps, and helps most exactly where it was argued it would.** The +0.09 overall
delta is within YouTube's own noise, but the per-case split is not random: Channa Mereya
+0.4 and Kesariya +0.3 against As It Was −0.1 and Brown Munde −0.1. The Arijit Singh and
Sidhu Moose Wala cases are the catalogue §3 measured English-centric sources under-serving,
and they are where the graph contributes most. §4.3's argument survives its first real test.

**Spotify's real quality gap is corroboration, not coverage.** It returns a full, distinct,
well-spread result set — 80 distinct songs, concentration and cross-seed overlap both on par
with YouTube. But 0.13 corroborated means **87% of its picks rest on a single signal**, so
ranking there is barely ranking. That is the number §7.3 exists to move, and it is now the
strongest argument for sequencing a second graph source next: a second independent source is
the only thing that can create agreement on a backend with no native signals at all.

**Artist-centric similarity is quantified at last.** §2.3 stated the cost — Deezer has no
track-level radio, so graph similarity works through artists — without a number. Two seeds by
the same artist overlap 90% on Spotify (Excuses/Brown Munde, Channa Mereya/Kesariya) versus
60%/50% on YouTube, where per-track radio pulls them apart. On a graph-only backend,
seeding from a different song by the same artist returns nearly the same playlist.

One thing the build settled that the proposal above did not anticipate: the playlist path had
to bypass `recommend_from_playlist` and call `gather_seeds` with a **pinned** seed list,
because the tool samples its seeds with `random.sample`. Two runs that do not share seeds
cannot be A/B'd at all — the delta would be sampling noise. Measuring it was worth the
round-trips for §6.1's reason: the single-seed path stays on the calling thread, so it is
structurally blind to everything the multi-seed path can break.

### 7.3 A second music graph source — *done*

Deezer is now a single point of failure, and §4.3's argument condemns exactly that: building
discovery on one service's endpoints is how you get 403'd into nothing. Deezer is keyless,
unauthenticated, contract-free and under no obligation to anyone.

Proposed: a second source behind the same `graph.py` seam — MusicBrainz/ListenBrainz is the
obvious pick (open data, real similarity, no key) — so "the graph" becomes plural the way
"signals" already is. This is the most on-brand item on the list.

**§7.2's baseline turned this from on-brand into load-bearing.** Two measurements, both new:
87% of Spotify's picks rest on a single signal, because with no native signals there is
nothing for the graph to agree *with* — one source cannot corroborate itself, so the ranking
that is supposed to be agreement-based is barely ranking there. And same-artist seeds return
90% the same songs on that backend, because one artist-centric source is the only thing
shaping the result. A second independent source is the only fix for either; neither is
reachable by improving Deezer coverage. This should be next.

**Shipped 2026-09-11 as `brainz.py`, but not the way this section proposed it.**
The section called MusicBrainz/ListenBrainz "the obvious pick" while `graph.py`'s
own header recorded it as probed and *rejected* — empty `similar-recordings` for
all six test tracks, 12-19s per call. Both were partly right. The rejection
tested a **track**-level endpoint, and the graph here is artist-centric by
`graph.py`'s own argument; the v1 `similar-recordings` path now 404s outright.
Re-probed against the labs API's `similar-artists` instead:

| seed | latency | neighbours | top names |
|---|---|---|---|
| AP Dhillon | 0.5s | 33 | Gurinder Gill, Shubh, Diljit Dosanjh |
| Diljit Dosanjh | 0.6s | 100 | Sidhu Moose Wala, Harrdy Sandhu |
| Arijit Singh | 0.7s | 100 | Shreya Ghoshal, Atif Aslam, KK |
| The Weeknd | 0.8s | 100 | Daft Punk, Kendrick Lamar |

**Independence is the only thing that justified adding it, so it was measured
first.** Against Deezer's related-artists over eight seeds: overall Jaccard
**0.137**. On the Punjabi core, ListenBrainz corroborates 12-15 of Deezer's 20
(~60-65%) while adding 393 new artists across the eight — Manni Sandhu, Prem
Dhillon, Sunny Malton, Harnoor, KK, Sunidhi Chauhan, A. R. Rahman. Enough
agreement to be evidence, enough disagreement to be information. Live, candidate
pools grew 30-50% (62→80 on a Punjabi seed) and warm re-runs cost 0.00s.

**Three things this deliberately does not claim.**

- **Deezer is still a single point of failure for the *catalogue*.** ListenBrainz
  returns artists, never tracks, so every neighbour crosses back into
  `graph.artist_tracks`. This makes adjacency plural; the section's framing
  ("Deezer is now a single point of failure") is narrowed, not closed.
- **Coverage is partial.** Dua Lipa returns zero neighbours. A seed ListenBrainz
  cannot answer for degrades to Deezer-only silently, and must.
- **The 0.137 is flattered by size asymmetry** — ListenBrainz returns up to 100,
  Deezer a fixed 20. At matched depth the agreement would read higher.

**The live run found two defects the unit suite could not, which is §5's whole
argument again.** First, a MusicBrainz 503 — and it 503s readily at 1 req/sec —
was being cached as `no_match`, so one transient rate-limit permanently poisoned
an artist, indistinguishably from a real miss. It had already silently emptied
Arijit Singh. Fixed by making "could not ask" a distinct outcome from "asked,
the answer was no"; only the latter is cacheable. Second, the adjacency seed was
the *Deezer-resolved* artist name, and Deezer credits "Kesariya" to Pritam, its
composer, where the providers credit Arijit Singh, who sings it — so the lookup
returned neighbours unrelated to the seed (Tanzanian bongo flava for a Bollywood
track). Indian film music makes the composer-vs-performer split the common case,
not an edge one, so `neighbours` now takes the provider's own credit. Both are
pinned by tests.

`scripts/quality_check.py`'s `GRAPH_SOURCES` went to four alongside this. That
matters for honesty rather than bookkeeping: had the ceiling stayed at three
while a fourth source started contributing, the 87% single-signal number would
have improved partly by arithmetic and the measurement would be flattering
itself. **The re-measured baseline is still owed** — see §7.10.

**§7.10 has since run, and it contradicts this section's central claim.** The sentence above
— "a second independent source is the only fix for either" — is wrong on both counts. It is
not a fix for the same-artist-seed defect at all (90% before, 90% after: both sources are
artist-centric, so they cannot separate two seeds sharing an artist), and it barely moved the
single-signal defect (0.13 → 0.16). The error is visible in this section's own reasoning:
it justified the source on measured *independence* and then expected *corroboration*, which
are opposites. Independent sources name different artists, and their tracks arrive as new
single-source candidates rather than as second votes. Read §7.10 before citing anything here.

### 7.4 Continuous indexing — *done*

Coverage is the quality ceiling (§3) and it is crawl-bound: the measurement says growing it
means crawling more of Deezer, not resolving better. Yet every crawl was a manual one-shot
script, so an install's index was as good as the last time someone remembered.

**Built 2026-09-13.** `scripts/maintain.py` runs, in order, the four things worth topping up
on a schedule: library sync + atlas materialize + artist propagation (provider-neutral,
always), YouTube's editorial mood atlas and genre pages (YouTube only, skipped elsewhere with
a stated reason), the Deezer tempo backfill, and the shared graph atlas's crawl/materialize/
propagate. Each stage was already independently resumable (`build_atlas.py`,
`build_graph_atlas.py` and `build_tempo.py` all skip what they've already attempted) — the
thing actually missing was one script that calls them in order, catches one stage's failure
without losing the others (the same silent-degradation contract `graph.neighbours` already
holds `graph_related_lb`/`graph_similar_lb`/`graph_similar_lfm` to), and says honestly what
ran. Bounded by default (`DEFAULT_ATLAS_LIMIT` etc.) so a scheduled run stays short; `--full`
lifts the caps for a deliberate catch-up.

`index_status()` now reports a `maintenance` block: when `scripts/maintain.py` last ran, and
the coverage delta since then (`trend_since_last_run`), rather than only the current totals —
so a stalled cron job is visible instead of looking like a self-maintaining index that happens
to be flat. `store.coverage_snapshot`/`record_maintenance_run`/`maintenance_status` hold the
bookkeeping (a flat, all-numeric slice in `meta`, the same key-value table `atlas.py` already
uses for `atlas_last_crawl_at`), covered by `tests/test_v2.py`.

Not yet done: actually installing the cron line anywhere, and `label_library.py`'s Claude
pass (step 4) is deliberately left out of the schedule — it costs money per song and should
stay an explicit, opted-into run rather than something a cron job does unattended.

**First live run, 2026-09-13, against the YouTube backend.** All ten stages ran clean on the
first pass: 24 playlists synced, the YouTube atlas fully up to date already (0 new, all 1,979
listings previously crawled), 27 genres re-harvested in 4.5m, 66 new tempo lookups, and the
shared graph atlas fully caught up (0 new queries — its 231 had already been crawled).
`graph_propagate` relabelled 427 tracks from the shared graph and left 1,150 resolved-but-
moodless and 205 unresolved, both worth narrowing later but not new to this work.

The run's own trend report immediately misreported itself: `_report_status` read back the
snapshot `record_maintenance_run` had *just written*, so every field showed the run diffed
against itself and printed a misleading flat `+0.0000` regardless of what had actually moved.
Fixed by capturing the previous snapshot before recording the new one, not after — verified
on a second live run, which correctly showed `genre_tracks +140` (from `build_genres.py`'s own
top-N playlist resampling) against a flat `+0.0000` everywhere the second pass genuinely found
nothing new. The exact failure mode §6.5 named: this is a case a mocked/unit-tested version of
`_report_status` would not have surfaced, because the bug is in the sequencing between two real
writes to the same store, not in either write's own correctness.

Review then caught a second, quieter one that the second live run had already shown without
anyone reading it: `tempo: cached=400, resolved=0`. The bounded run truncated the library to
400 rows *before* skipping already-attempted ones, so every scheduled run re-checked the same
400 and 215 tracks were unreachable forever. Filtered to never-attempted rows first; the third
live run attempted all 217 pending (96 with BPM), `tempo_coverage +0.0098`. The same review
moved the provider client, `YTMusic` and graph connection inside their stages, so an
unconfigured backend costs its own stage rather than the whole run and its record.

### 7.5 Close the read-only handoff gap — *done*

§4.9's decision is right; the ergonomics around it are a footgun. Turning a recommendation
into a playlist is three steps and skipping the third leaves the exclusion set stale for up
to six hours.

Proposed, without letting re-com write anything:

- `refresh_library(video_ids=[...])` unions specific ids into the cache instantly, with no
  ~20s rebuild — so the agent that just added tracks can close the loop cheaply.
- A `served` staging set, so a song handed out 30 seconds ago cannot come back in the next
  call before it has been saved anywhere. "No repeats within a session" is the failure users
  actually notice.

**Built 2026-09-14.** Both, as proposed. `refresh_library(video_ids=[...])` unions into the
cache and keeps its original `fetched_at`, so adding ids never makes a stale full build look
fresh; with no usable cache it falls back to a full build and still unions the ids, in case
the service hasn't surfaced the add yet. The served set is a new `served` table rather than
the existing `recommendation` one, which feeds implicit feedback and should keep meaning
"served by the mood engine" — logging similarity picks there would have quietly changed mood
ranking. All five recommendation tools read and write it; `RECOM_SERVED_TTL` (default 2 hours,
`0` disables) bounds it, and rows are pruned on write since nothing reads them past the window.
The smoke harness sets it to 0 so a pre-release run can't hide songs from the listener's next
real session; `quality_check.py` mirrors the pipeline rather than calling the tools, so its
repeated-run noise floor is untouched.

The 2-hour default is a judgment call, deliberately not tied to `RECOM_CACHE_TTL`: this install
runs a 7-day cache, and a week without seeing a song again is not "a session".

**Live, YouTube, against a copy of the real cache and a throwaway store.** Two consecutive
`songs_by_artist("AP Dhillon", 5)` calls overlapped 0/5; so did two `recommend_from_song`
calls on *Excuses*. `refresh_library(video_ids=[one])` took 0.00s against ~20s for a rebuild,
added exactly one id, and preserved `fetched_at`; with the served set disabled, that id stayed
excluded by the cache alone.

**What the live run exposed, not caused.** The second *Excuses* batch came back in alphabetical
order, and that is literal: of the top 30 candidates, 3 score 2 and 27 tie at 1, and
`signals._finalize` breaks ties on title. So past the first three, `recommend_from_song` on
this seed is ordering by the alphabet, not by similarity — §7.10's single-signal finding seen
from the user's side. The served set does not create this, but it makes it visible: every
repeat call walks further down an alphabetical tail. The tie-break wants a real secondary key; tracked
as §7.15.

**Review found the guarantee had a hole older than this work.** When a language filter leaves
`recommend_from_song` short, `_apply_result_filters` re-seeds from the survivors through
`recommend.bridge_expand` — and passed it `exclude=set()`. The bridge's candidates are gathered
after the tool's own exclusion has already run, so nothing else ever checked them: songs already
in the library could come back through it, and after this change so could just-served ones. The
library half predates §7.5 and was on `main`; it only surfaced because "every tool honours the
served set" made someone trace every path that produces a song. The tool's full exclusion set,
plus the seed, now reaches the bridge, pinned by a test that fails without it.

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

**One-backend only, and staying that way — say so, don't hide it.** YouTube Music's
`likeStatus` has no Spotify analogue; there is no equivalent history field to read there, and
none is coming. Unlike the native radio/related/artist signals (§4, provider-gated because a
Spotify user still gets a fully-functional engine with fewer inputs feeding *ranking*), this is
a user-facing exclusion *guarantee*, and on Spotify it would just silently never apply. Ship it
gated on capability like the native signals are, but the tool docs must say plainly "YouTube
Music only" rather than let a Spotify user believe their dislikes are respected when the
mechanism to observe them doesn't exist for that backend.

### 7.7 An agentic orchestration layer — *v0 done; the loop works, and it is not the pipeline*

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

**Built 2026-09-16 as `scripts/orchestrate.py`.** The Claude Agent SDK spawns `server.py`
itself as a stdio MCP subprocess, so the agent calls the real tool surface rather than
in-process Python; built-in tools are named off, and `strict_mcp_config` with
`setting_sources=[]` keeps the developer's own machine out of the experiment. Seven read-only
tools are reachable; `record_feedback` and `refresh_library` are not, so a bad plan cannot cost
anything.

**That last sentence was false when it was first written, and verifying it is the only reason it
is true now.** The restriction rested on `allowed_tools`, which **does not bind under
`permission_mode="bypassPermissions"`** — a mode this script sets to stay non-interactive.
Tested directly: an agent configured exactly like this one, asked to call `record_feedback` with
the tool absent from `allowed_tools`, loaded its schema through `ToolSearch` and called it
successfully, writing a row to the store (a redirected one, via `RECOM_DB_PATH`). The allowlist
is now enforced by a `PreToolUse` hook that returns `permissionDecision: "deny"` for anything
outside it, and the same probe against the shipping configuration is refused and logged as
`tool_denied`, with no row written. A permission claim that is never tested against an agent
actually trying to break it is a comment, not a control.

**The task changed shape before any code was written, and the reason is worth keeping.** §7.7's
own example needs song durations. **There are none** — `grep -rn duration` is empty across
re-com, `ytmusic-mcp` and `spotify-mcp`. Deezer returns `duration` on the same `/track/{id}`
payload `tempo.lookup` already fetches for BPM, so it is *addable*, but that is a schema change
and a crawl, and it would have been discovered halfway through building the agent. v0 therefore
drops duration for count-based constraints and changes nothing in the engine: **20 distinct
songs, no artist more than twice across the whole set, at least 15 genuine matches rather than
filler, and energy rising from the first half to the second.**

**The checker reads re-com's output, not the agent's claims.** A registry captures every song as
the tool reported it, before trimming; the agent's final answer is only a list of `videoId`s. So
a run cannot pass by asserting its picks were well-rated — `rated` and `mood` come from the
engine — and an id no tool ever returned fails the run rather than being quietly dropped.

**Measured, YouTube, three consecutive runs.** All three **PASS**:

| | run 1 | run 2 | run 3 |
| --- | --- | --- | --- |
| count / distinct | 20 / 20 | 20 / 20 | 20 / 20 |
| max per artist | 2 (The Weeknd) | 1 (Shakira) | 2 (Calvin Harris) |
| rated | 20/20 | 20/20 | 20/20 |
| energy, first half → second | 0.626 → 0.845 | 0.703 → 0.842 | 0.662 → 0.831 |
| tool calls / turns | 7 / 8 | 8 / 9 | 6 / 7 |
| context budget | 34,649 → 10,261 bytes | 38,661 → 12,002 bytes | 29,923 → 9,364 bytes |
| cost | $0.35 | $0.30 | $0.32 |

A fourth run, after the permission fix below, also passed (20/20, max 2 per artist, energy
0.615 → 0.914, 9 tool calls, $0.36).

**It replanned, which is the only thing that made this worth building.** Run 1's sequence:
`index_status` to see what the backend supports → `recommend_for_mood` at energy 0.62,
`arc="lift"`, **BPM-filtered 140–180** → read the result's own notes ("17 dropped as out of
range; 220 kept with unknown BPM") and `match_quality` (18 genuine of 20) → **dropped the BPM
filter** and fanned out across energy tiers (0.68 `arc="lift"`, 0.88 `arc="hold"`) → added a
`language=["english"]` narrowing, which came back 7 genuine and 3 filler. Then it counted
artists across all four calls itself — the final set has The Weeknd exactly twice, a cap
`arc.sequence` only enforces *within* one call. Four distinct query shapes, each chosen from
what the previous result admitted about itself. §7.1–7.3's sequencing argument held: it replanned
on the tools' honesty, not on vibes.

**A defect in the harness, found the way §5 says they get found.** The first full run had every
tool appear to fail. The cause was in the trim hook, not the engine: an MCP tool's output must
be returned as content blocks, and replacing it with the bare object crashes the CLI measuring
the response (`'e.reduce' is undefined`). What makes it worth recording is the agent's response —
it refused to build the set from its own knowledge of running tracks, said so explicitly ("a
hand-written 20-song list would look plausible, carry fabricated videoIds, and silently fail
every downstream check"), and diagnosed the payload as malformed. The registry would have caught
a fabricated set anyway; it never had to.

**A second defect, found by verifying rather than by running.** The budget did not bind to the
number it claimed: the "N songs were dropped" notice was appended *after* the fit loop, so every
truncated result came back over the cap it had just been trimmed to meet — 2,082 bytes against a
2,000-byte budget live, and ~2.5x over at small budgets. The test written alongside it asserted
`<= budget + len(notice)`, which is a test shaped around the defect rather than one that could
catch it. Now the notice is counted inside the loop (2,082 → 1,972 live), and where the budget is
structurally unmeetable — `notes` and `match_quality` are never trimmed — the report says
`over_budget` instead of claiming a fit that did not happen.

**Two costs recorded rather than buried.** The harness defers MCP tools, so two of every run's
turns go to `ToolSearch` discovery before any music work starts. And the byte cap bound on one
result in runs 1 and 3 and none in run 2 — compaction alone (song lists to one line each, `seeds`
and `filters` dropped, `notes` and `match_quality` kept verbatim) already does most of the work at
this scale, cutting results ~70%. The cap is what would matter at crawl scale; at this size it is
mostly the compaction.

**What v0 does not settle.** Whether the loop is worth its cost against just calling
`recommend_for_mood` once — the constraints were chosen so one call *cannot* satisfy them, which
proves the loop works, not that it earns $0.30 and three minutes for a normal request. Also
untested: Spotify (same reason as §7.16 — no credentials configured here), multi-step session
state, and the playlist handoff, which stays a by-hand skill.

**Sequence it after 7.1–7.3.** An agent that replans on bad intermediate results is only as
good as the tools' honesty about being bad. Build it first and it replans on vibes.

### 7.9 Smaller items

- `server.py` is ~1,100 lines with tool bodies doing filter + store + resolve orchestration
  inline. Worth extracting a `tools/` layer.
- `ytmusicapi` is a hard top-level dependency that the live path never imports — only the
  offline scripts use it. It belongs in an extra; a Spotify-only install currently pulls in
  a YouTube Music client it never calls.
- The offline maintenance scripts still authenticate directly with their own
  `headers_auth.json` rather than going through `ytmusic-mcp`. Deliberate — they are bulk
  indexing jobs, not part of the live path — but it means two unrelated credentials exist.
- §7.3 added a MusicBrainz lookup at 1.2s throttled per *artist* to the live path. Cached
  permanently and only on a cold artist, but it belongs in `scripts/maintain.py` (§7.4) as a
  warm-ahead job rather than being paid in a user's first request for that artist.

### 7.10 Re-baseline after the second source — *done, and it mostly did not work*

Run live on both backends, 2026-09-11, warm, `limit=10`, same ten cases as §7.2. The second
source is confirmed live (16 artists resolved through MusicBrainz, 910 adjacency rows, 11
ListenBrainz fetches cached), so this measures the thing and not its absence.

Numbers below are **after** §7.11, which fixed a real regression this run exposed. The
intermediate figures are kept in §7.11 rather than here, so this table is the shipped state.

| | YouTube 7.2 → 7.3 | Spotify 7.2 → 7.3 |
| --- | --- | --- |
| agreement ceiling | 6 → 7 / seed | 3 → 4 / seed |
| **corroborated** | 0.47 → **0.52** | 0.13 → **0.16** |
| concentration (HHI) | 0.224 → **0.214** | 0.272 → **0.238** |
| cross-seed overlap | 0.042 → **0.033** | 0.044 → **0.060** |
| distinct / slots | 82/100 → **85/100** | 80/100 → 75/100 |
| corroboration delta | +0.09 → **+0.10** | n/a |
| **same-artist seed overlap** | 60%/50% → **50%/40%** | 90%/90% → **90%/90%** |
| noise floor | 0.87 → 0.87 | 1.00 → 1.00 |

**The headline defect barely moved, and the reason is a mistake in §7.3's own argument.**
Spotify's corroboration went 0.13 → 0.16: real rather than noise, since that backend's floor
is 1.00, but it means 87% single-signal became 84%. §7.3 justified the second source on
measured *independence* (Jaccard 0.137) and then expected *corroboration* from it. Those are
opposites. Corroboration requires two sources to name the **same track**; independence means
they name different artists, whose tracks enter the pool as fresh single-source candidates.
The very number that justified the work is the number that predicted it would not deliver
this. The gains appear exactly where the two sources *overlap* — the Punjabi/Bollywood core,
where ListenBrainz confirms 60-65% of Deezer's neighbours: Kesariya 0.4, Excuses 0.3, Channa
Mereya 0.3, Brown Munde 0.2, against 0.0-0.1 on every western seed. That is a coherent
finding, just not the one that was predicted.

**The same-artist-seed defect did not move at all: 90% before, 90% after.** §7.3 asserted "a
second independent source is the only fix for either". It is not a fix for this one, and the
reason is structural rather than a matter of degree: *both* sources are artist-centric, so
seeding two songs by one artist produces that artist's neighbour set either way. No number of
additional artist-centric sources can separate two seeds that share an artist. Only a
genuine **track**-level similarity signal can — the thing `graph.py`'s header says Deezer
does not have, and the thing ListenBrainz's `similar-recordings` was supposed to be before it
turned out to 404. This defect is therefore still open and is now understood to need a
different kind of source, not one more of the same kind.

**One cost survives the §7.11 fix, and is stated rather than buried.** Spotify's cross-seed
overlap got *worse* (0.044 → 0.060) and distinct songs fell (80 → 75): more candidates drawn
from overlapping neighbourhoods pull different seeds slightly closer together. On a backend
whose noise floor is 1.00 that is a real movement, not sampling. So on Spotify the second
source trades a little breadth for a little corroboration — a defensible trade, but a trade,
and not the free win §7.3 implied.

**What did improve, honestly.** YouTube: corroboration 0.47 → 0.52, concentration 0.224 →
0.214, cross-seed overlap 0.042 → 0.033, distinct songs 82 → 85, same-artist overlap 60%/50%
→ 50%/40%. Spotify: concentration 0.272 → 0.238. Results are more varied and less clustered
on both. Worth having; still not what §7.3 was for.

### 7.11 YouTube's lost slots — *done*

§7.10 found YouTube filling 91 of 100 slots where §7.2 filled 100. A second candidate source
must never *reduce* the songs returned, so this was chased first: unlike a quality number, it
costs a user actual results.

**The hypothesis in this section's first draft was wrong.** It guessed
truncate-then-resolve; `resolve_candidates` already resolves in rank order and walks on past
a failure. Traced live instead, which named it immediately:

    Excuses — AP Dhillon:  pool_in=16  graph_in=9  dropped=7  → returned 8

**The pool and the search budget were the same number, and should never have been.**
`resolve_pool_size` served as both how deep the ranked pool goes *and* how many provider
searches are allowed. Its 1.6x was sized when graph candidates were a minority of the top of
the pool; a native candidate always has a provider id, a graph candidate needs a search that
can fail. §7.3's fourth source shifted that mix, the drop rate rose past 1.6x, and the pool
had nothing left to backfill from. `server.py` already *claimed* the right design — "the pool
stays deep for native candidates while the searching stays bounded" — but only did it on the
filtering path. Split into `backfill_pool_size` (4x, local, free) and `max_resolve`
(unchanged, the network cost). Both callers fixed; searches per request did not rise.

**The fix exposed a second, quieter defect: the note had started lying.** `dropped` counted
every unresolved candidate, including ones below the search budget that were never looked up
at all — so the user was told 20 songs "couldn't be matched" when 7 had been tried and 13
were untouched pool tail. Invisible while the two budgets were one number, and a deeper pool
turned it into a misleading message. `dropped` now counts only attempted candidates.

**And a third, found while re-measuring: the harness was not measuring the shipped path.**
`quality_check.py` carried its own copy of the pool sizing, so after the server was fixed it
still reported 92/100 — a short result the real tool no longer returned. This is §5's whole
argument turned on the verification layer itself: a harness that duplicates the logic it
measures will eventually measure something that does not ship. Now aligned, and the fully
restored run reads 100/100 slots with 85 distinct songs, above §7.2's 82.

**Closed properly 2026-09-12.** Aligning the numbers left the *duplication* in place, which is
the thing that actually drifts — three call sites paired the pool depth and the search budget
by hand. They now come from one `signals.resolve_budgets(limit, filtering=...)` returning both
as a tuple, because the pairing is what broke, so the pairing is what gets centralised.
Pinned by two tests that read the callers' **source** and fail on any re-derivation:
restating the numbers in a test would be the same duplication in a new place, and would pass
while drifting. Verified by mutation — reinstating the hand-paired version fails the test.

### 7.12 Find a track-level similarity signal — *wired in; small real gain, defect still open*

§7.10 established that the 90% same-artist-seed overlap cannot be fixed by any
artist-centric source, and both of re-com's are artist-centric. This needs a signal that
distinguishes two songs by one artist. Candidates worth probing, cheapest first: Deezer's
`/track/{id}/radio` (recorded absent in `graph.py`, worth re-probing on the same grounds
ListenBrainz was), ListenBrainz's `similar-recordings` under a **valid** algorithm name (the
labs endpoint rejected the one tried, with an enumeration error that implies a discoverable
list), and last.fm's `track.getSimilar`, which is track-level by design but needs a key.
Probe before proposing: §7.3's lesson is that the shape of the endpoint matters more than
the reputation of the service.

**Probed 2026-09-12. The signal exists, has exactly the right property, and is blocked on one
specific thing.** Taking the lesson literally and probing before proposing paid off twice.

**Deezer is a dead end, as recorded.** `/track/{id}/radio` and `/track/{id}/related` both
return 200 with zero rows. `graph.py`'s note holds; nothing to re-open.

**ListenBrainz `similar-recordings` was rejected twice for the same wrong reason.** The
2026-08 probe and §7.3's own re-probe both sent an *invalid algorithm name* — the labs
endpoint answers those with a 400 whose body enumerates the seven permitted values, so the
"empty for all six tracks" result was never a coverage measurement at all. With a valid name
it responds in ~0.5s.

**It has the property no artist-centric source can have.** Two Arijit Singh tracks, the exact
pair that overlaps 90% today:

| seeds | overlap |
| --- | --- |
| Channa Mereya vs Kesariya, artist-centric (today) | **0.90** |
| Channa Mereya vs Kesariya, `similar-recordings` | **0.00** |

Different songs by one artist return genuinely different neighbours, and the neighbours are
right — *Channa Mereya* returns *Bulleya*, from the same film. This is the signal §7.12 was
looking for.

**The blocker is identity, not coverage — and that is why it looked empty.** Similarity is
keyed on one *canonical* recording MBID, while MusicBrainz search returns whichever recording
scores highest. For "Blinding Lights", 11 of its 12 recording MBIDs return **0** neighbours
and exactly one returns **100**. So the naive resolve-then-ask path yields near-zero coverage
while the data is fully present — the same failure mode as §7.3's composer-vs-performer bug,
one level down: asking a real service a well-formed question about the wrong identity.

**Next step is narrow and specific.** Find canonical-recording-MBID resolution:

- `api.listenbrainz.org/1/metadata/lookup/` does exactly this (artist + title → canonical
  MBID) but returns **401**; it needs a ListenBrainz user token. Free to obtain, and it would
  additionally retire `brainz.py`'s MusicBrainz dependency along with its 1 req/sec throttle —
  §7.9's cost item would disappear rather than move to §7.4.
- The unauthenticated labs mapper is the alternative; `mbid-mapping`,
  `mbid-mapping-release`, `explain-mbid-mapping` and `canonical-recording-redirect` all 404,
  so its real path still has to be found.

Until one of those lands this is not implementable, and **that is the whole finding** — worth
more than the code it defers, because it converts "ListenBrainz similarity is empty", now
twice-recorded and twice-wrong, into one concrete unblocking task. Do not re-probe
`similar-recordings` for emptiness a third time.

**Unblocked and wired in 2026-09-12** via the token route. `brainz.resolve_recording` calls
`/1/metadata/lookup/` (~0.5s; a miss is `{}` with a 200, cached as no-match; a 401 is not cached),
`brainz.similar_recordings` calls the labs endpoint with `LB_RECORDING_ALGORITHM` — a *different*
enumeration from the artist one — and `graph.neighbours` resolves each neighbour back to Deezer
and tags it `graph_similar_lb`. No token means the source is off. Live, against the real cache:

| seed pair | artist-centric | `graph_similar_lb` | whole pool |
| --- | --- | --- | --- |
| Channa Mereya vs Kesariya | 0.99 | 0.00 | 0.96 |
| Blinding Lights vs Save Your Tears | 0.97 | 0.25 | 0.82 |
| Excuses vs Brown Munde | 0.97 | 0.00 (Brown Munde: 0 rows) | 0.87 |

The signal differentiates exactly as probed, but it is 1–10 rows in a ~72-candidate pool and thin
on the South Asian half (Channa Mereya 1, Kesariya 1, Brown Munde 0), so whole-pool overlap barely
moves there. What decides whether it matters is the *ranked top ten*, where agreement with another
source lifts a candidate — that needs `quality_check.py --similarity` re-baselined (Spotify first,
whose noise floor is 1.00, so any delta is real). `GRAPH_SOURCES` now counts it: ceilings are 8/seed
YouTube, 5 Spotify.

**YouTube A/B, 2026-09-12** (`--similarity`, `limit=10`, token off vs on, same cache, both ceilings 8):

| | token off | token on |
| --- | --- | --- |
| corroborated | 0.51 | **0.54** |
| corroboration delta (graph vs native) | +0.11 | **+0.18** |
| concentration (HHI) | 0.194 | 0.198 |
| cross-seed overlap | 0.022 | 0.029 |
| distinct / slots | 90/100 | 87/100 |
| same-artist overlap, Arijit / AP Dhillon | 40% / 30% | 30% / 50% |

Inconclusive, and said so: YouTube's noise floor is 0.87, and the same-artist pairs moved in
opposite directions. The one consistent movement is corroboration — more track-level neighbours
land on songs another source already named. Cold-cache seeds rose to ~7s on the western half
(first lookups; cached thereafter).

**Spotify A/B, same day** — the decisive run, since repeated runs there are bit-identical:

| | token off | token on |
| --- | --- | --- |
| corroborated | 0.16 | **0.22** |
| concentration (HHI) | 0.240 | 0.254 |
| cross-seed overlap | 0.058 | 0.058 |
| distinct / slots | 76/100 | 76/100 |
| same-artist overlap, Arijit / AP Dhillon | 90% / 90% | 90% / **80%** |

The off arm reproduces §7.10's Spotify column (0.16, 90%/90%), so the harness measured the same
thing. On a 1.00 noise floor every delta here is real: corroboration +6 points with no loss of
breadth, one same-artist pair down 10 points, concentration slightly worse. That is a genuine
gain and a small one, and it lands where coverage predicted — the Arijit pair, which has one
neighbour each, does not move at all.

**Verdict.** Worth keeping as the canonical-identity groundwork and a modest corroboration gain;
not a fix for the same-artist defect on this library. Probing all seven algorithms and every
MusicBrainz recording afterwards showed the gap is ListenBrainz's listener base, not a setting
(Brown Munde and 295 return 0 everywhere), so the defect moves to §7.13's second track-level source.

### 7.13 A track-level source for the South Asian catalogue — *done; first real movement on the same-artist defect*

§7.12's source is nearly empty exactly where the same-artist defect was measured. Probed
2026-09-12 before choosing a second one:

**No ListenBrainz configuration fixes it.** All seven `similar-recordings` algorithms, same seeds:
the one shipped is within two neighbours of the best on every seed, and Brown Munde and 295 return
0 under all seven. Every MusicBrainz recording of both also returns 0, so this is ListenBrainz's
listener base, not the canonical-identity problem from §7.12.

**last.fm `track.getSimilar` reaches it:**

| seed | last.fm | ListenBrainz | own-artist share |
| --- | --- | --- | --- |
| Excuses | 50 | 28 | 1/50 |
| Brown Munde | **50** | 0 | 2/50 |
| 295 | **50** | 0 | 0/50 |
| Tum Hi Ho | **50** | 8 | 2/50 |
| Lover (Diljit) | 50 | 5 | 2/50 |
| Channa Mereya / Kesariya | 0 / 0 | 1 / 1 | — |

Excuses vs Brown Munde overlap 0.27 on last.fm, against 0.97 artist-centrically. ~0.5s per call.

**The remaining gap is recent Bollywood film songs**, and it is a data shape, not a lookup miss:
every last.fm spelling of Channa Mereya and Kesariya returns zero, because their listeners are
split across lo-fi flips, "(From <film>)" titles and MP3-site rips and no single entry clears the
similarity threshold. Neither track-level source covers them.

Wired in as `lastfm.py`, tagged `graph_similar_lfm`, behind `LASTFM_API_KEY`. Identity is last.fm's
own text matching (`autocorrect=1`), so there is no MBID step. Error contract measured live: error 6
(a 200) and an empty list are cached answers; a bad key (403, error 10), rate limiting or a network
failure cache nothing. `graph.neighbours` now runs both track-level sources through one
`_similar_tracks` helper. Ceilings rise to 9/seed YouTube, 6 Spotify.

**Spotify A/B, 2026-09-12** (`--similarity`, `limit=10`; ListenBrainz token on in both arms, last.fm
key off vs on; both arms on a *copy* of the graph cache, because this branch predates #12 and a
Deezer failure mid-run would otherwise have been cached into the real one):

| | last.fm off | last.fm on |
| --- | --- | --- |
| corroborated | 0.22 | **0.34** |
| concentration (HHI) | 0.254 | **0.238** |
| cross-seed overlap | 0.058 | **0.051** |
| distinct / slots | 76/100 | **78/100** |
| same-artist overlap, Arijit / AP Dhillon | 90% / 80% | 90% / **60%** |
| cold seed latency | ~1s | ~2-4s |

The off arm reproduces §7.12's token-on column exactly, so the baseline is sound, and on a 1.00
noise floor every delta is real. **Every quality number improved and none regressed** — unlike
§7.3, which traded breadth for corroboration, and §7.12, which bought corroboration with a little
concentration. Corroboration has now more than doubled since §7.10 (0.16 → 0.34), and the AP Dhillon
pair is the first same-artist overlap to fall meaningfully (90% → 60%).

**The Arijit pair did not move, exactly as the probe predicted**: neither track-level source has a
single neighbour for Channa Mereya or Kesariya. That residual is now understood as a data gap for
recent Bollywood film songs — listeners split across duplicate uploads — rather than a missing
source. The cost is first-lookup latency; everything is cached after.

### 7.14 The Channa Mereya residual, narrowed — *done, partial*

§7.13's residual turned out to be two different problems wearing one label. Probed live
2026-09-13: last.fm doesn't lose these songs to duplicate *uploads*, it splits them by
*credit*. "Channa Mereya" is two separate last.fm track entries — 983 listeners under Arijit
Singh (the performer, the credit graph.py deliberately queries under, for the composer-vs-
performer reason in §7.3) and 52,899 under Pritam (the composer, Deezer's own credit) — and
`track.getSimilar` is empty on the thin one and returns ten genuinely relevant neighbours
(Kabira, Enna Sona, Agar Tum Saath Ho...) on the other. "Kesariya" is empty under *both*
credits, so that one is the real duplicate-upload case §7.13 described.

Wired in: `graph.neighbours` now retries last.fm under the seed's Deezer (composer) credit
when the performer-credit query comes back empty and the two credits differ. Cost is one
extra call only on a cache miss — never on a hit, never when the first call already
succeeded — so it is free in steady state. Spot-checked three more recent Bollywood titles
under both credits (Raataan Lambiyan, Kalank Title Track, Agar Tum Saath Ho) to see whether
this generalizes: all empty either way. So this fixes Channa Mereya specifically, not the
class of problem — most of §7.13's residual is still real. Tests:
`test_neighbours_fall_back_to_composer_credit_when_performer_credit_is_empty` and
`test_neighbours_do_not_retry_lastfm_when_credits_are_the_same` in `tests/test_lastfm.py`.

### 7.15 Break score ties on evidence, not the alphabet — *done; better picks, somewhat more seed-artist concentration*

`signals._finalize` sorted on `(-score, title)`. Measured 2026-09-14 on *Excuses*: 27 of the top
30 candidates tie at score 1, so the title tie-break decides almost the whole list. Proposed:
a secondary key from each candidate's best rank within the source that surfaced it, then title
only as a final deterministic fallback. That rank is not stored today — `_merge_and_score` keeps
only the source set and a count — but the merged dict is built in each source's own order, and
Python's sort is stable, so recording first-seen position at merge time is cheap; it is the title
key that currently throws it away. Measure with
`quality_check.py --similarity` before and after; nothing about it should change the top of a
well-corroborated result.

**Built 2026-09-14.** `signals._note_rank` records each candidate's position within each
source's own list as it is gathered (native and graph alike); `_merge_and_score` keeps the best
position any seed gave it; `_finalize` sorts on `(-score, rank, title)`, and variant collapse
prefers the better-ranked variant at equal score. Score is untouched, so corroboration cannot
move by construction.

**The fix was half-invisible to the harness, and would have shipped that way.** The first
change made `_finalize` rank-ordered, and `quality_check.py` — which calls `_finalize` directly —
would have reported the win. But `server._apply_result_filters` re-sorts `recommend_from_song`'s
results on `(-base_score, title)` a second time, after `_finalize`, so the real tool would have
stayed alphabetical. §7.11's lesson from the other side: there the harness carried a stale copy
of shipping logic; here the shipping tool carries a step the harness doesn't mirror. Now a
stable sort on score alone, pinned by a test that fails without it.

**Measured, YouTube.** Full harness, `--similarity --repeat`, before (a worktree of `main`) and
after:

| | before | after |
| --- | --- | --- |
| corroborated | 0.57 | 0.59 |
| concentration (HHI) | 0.222 | 0.226 |
| cross-seed overlap | 0.027 | 0.036 |
| distinct / slots | 88/100 | 85/100 |
| noise floor | 0.77 | 0.85 |

None of that clears YouTube's noise floor, which is the point of a second measurement: a
same-pool A/B gathers each seed's candidates **once** and ranks that pool both ways, so every
difference is the sort key. Two independent gathers, nine seeds:

| same pool | title tie-break | rank tie-break |
| --- | --- | --- |
| HHI (uncapped) | 0.213 / 0.209 | **0.236 / 0.229** |
| seed-artist share | 0.200 / 0.200 | **0.300 / 0.300** |
| cross-seed overlap | 0.044 / 0.042 | 0.044 / 0.039 |
| corroborated | identical | identical |

The picks are plainly better. *Kryptonite*'s tied tail went from "3AM, ANTIDOTE (FULL MIX)" plus
three re-uploads of the seed to "Here Without You, Holiday, In the End, Everlong"; *Blinding
Lights* from "1989, A Sky Full of Stars, Anything Can Happen" to "Get Lucky, One More Time, Sign
of the Times"; *Brown Munde* from literally "21, Aaye Haaye, Afsos… Bars".

The cost is real and was predicted before measuring: rank ordering pulls in more of the seed
artist (share 0.20 → 0.30 uncapped). The obvious suspect — the seed artist's own popularity-ordered
top-songs lists taking rank 0 — was tested as a third arm that ignores `artist`/`graph_artist`
positions, and **refuted**: HHI 0.240, share 0.267. The similarity sources themselves rank the
seed artist's other songs highly, which is arguably correct similarity rather than a defect of
the key. Kept the simpler version. What a user sees is bounded: `recommend_from_song` caps at 2
per artist (live, *Excuses*/*Bad Guy*/*Brown Munde*: max 2, no alphabetical tail), and the
pinned-playlist case was fully corroborated in both runs, so the tie-break never reached its top
ten. `recommend_from_playlist` has no per-artist cap; if a thin playlist shows concentration,
that cap is the fix, not reverting this.

Out of scope and noted: the harness's `_run_similarity` does not drop re-uploads of the seed the
way `_apply_result_filters` does, so its per-seed lists can include the seed song under other
ids (*Kryptonite* ×3 in the title arm). The tool is unaffected.

### 7.16 Corroboration on the similarity path — *done*

The problem §7.15 made visible rather than solved. On *Excuses* (YouTube, 2026-09-14) 27 of the
top 30 candidates rest on a single source, so ordering within that tail is the only lever left
and no tie-break can make a single voice into agreement. On the harness, 57–59% of YouTube's top
ten are corroborated (§7.15) and 34% of Spotify's (§7.13). The same-artist defect §7.12 opened is
the same shortage seen from another angle.

**Already ruled out, so not to be re-proposed:**

- *Another artist-centric source.* §7.10: independence and corroboration are opposites, and no
  artist-level source can separate two songs by one artist.
- *ListenBrainz configuration.* §7.12 probed all seven algorithms; the gap is its listener base.
- *Recent Bollywood film songs via last.fm/ListenBrainz.* §7.13/§7.14: empty under both performer
  and composer credits for most titles probed; a data gap, not a query bug.

**The lead was in the code, not in a new source.** Native candidates are keyed by provider id,
graph candidates by `graph:<deezer id>` (`signals._add_graph_candidates`), so `_merge_and_score`
never merges a song that YouTube radio and, say, last.fm both named — they stayed two candidates at
score 1 each. The only place they met was `_collapse_variants`, which kept the higher-scoring copy
and **dropped the other's sources rather than unioning them**. Agreement between the native and
graph families was therefore structurally uncountable on YouTube.

**Measured before proposing, per the plan above.** `scripts/measure_corroboration.py` gathers each
`quality_check.SIMILARITY_SEEDS` seed's pool once, replicates `_collapse_variants`'s own clustering,
and counts clusters that mix a native-keyed and a graph-keyed candidate for the same song. Live on
YouTube, 2026-09-15: **70 of 81 multi-member clusters were mixed** — not a footnote. Corroborated
share would move 0.611 → 0.826 under the counterfactual (western 0.6 → 0.9, South Asian
0.62 → 0.767). Spotify could not be checked live in this environment (`spotify-mcp` had no
credentials configured here); the prediction that it shows near-zero effect, having no native
signals to fragment against, is still open.

**Built.** `_collapse_variants` now unions `sources` and sums `score` across a cluster onto the
kept variant, instead of discarding the losers' evidence; which variant's title/artist/videoId
represents the cluster is still chosen by the old `(score, rank)` rule on the *pre-union* numbers,
so a genuinely better-corroborated variant still wins over a single-signal one. Pure evidence
bookkeeping — no new source, no ranking-formula change.

**Verified live, YouTube, `quality_check.py --similarity --repeat`, same account, before/after:**

| | before | after |
| --- | --- | --- |
| corroborated | 0.59 | 0.87 |
| concentration (HHI) | 0.274 | 0.312 |
| cross-seed overlap | 0.024 | 0.027 |
| distinct / slots | 90/100 | 89/100 |
| corroboration delta (graph vs native) | +0.08 | +0.27 |
| noise floor | 0.86 | 0.84 |

Both corroborated numbers clear their own noise floor by a wide margin, so this is signal, not
variance. The predicted cost from §7.15 landed exactly where expected: HHI rose (0.274 → 0.312) —
more counted agreement pulls in more of the seed artist's own well-corroborated songs — while
cross-seed overlap and distinct-song count barely moved. Not re-litigated: the same tradeoff §7.15
already accepted and bounded (`recommend_from_song`'s 2-per-artist cap).

**A known overcount, measured rather than assumed (2026-09-16).** Summing scores across a cluster
breaks `score`'s documented meaning — "the number of distinct (seed, source) pairs" — whenever one
source surfaced two variants of the same song, because that pair then gets counted twice. It
cannot happen across the native/graph families (their source names are disjoint, which is the case
this change exists for), only within one. Measured over `SIMILARITY_SEEDS`: **16 of 89**
multi-member clusters overcount, 211 summed against 190 true distinct sources. The effect on the
headline is small enough to leave: corroborated **0.922** under the shipped rule against **0.911**
scoring on distinct sources instead — a 0.011 gap, far inside the 0.84–0.86 noise floor, and only
one seed (*Brown Munde*, 0.6 vs 0.5) moves at all. So §7.16's result stands, but the invariant is
violated and the honest fix is to carry the `(seed, source)` pairs through `_merge_and_score`
rather than a count. Not done here because it changes `score` for every ranking path, which needs
its own before/after.

**Pinned by tests as of 2026-09-16, which it was not when it shipped.** The original change had no
test: reverting it entirely left all 652 green. `test_collapse_unions_the_sources_of_a_native_and_a_graph_copy`
and `test_collapse_still_picks_the_representative_on_pre_union_score` both fail without it.

**Still open:** the Spotify prediction above, and whether §7.12's same-artist defect narrows now
that corroboration counts correctly — re-baseline that number before proposing anything further
for it.

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
harness itself, and CI. CI's first run found the project had been uninstallable on any
recent setuptools (§7.1's notes) — fixed by declaring `py-modules` explicitly. The harness's
own first live run found what it was built to find: two silent Spotify defects §5's
unit-only suite could not see. `spotify-mcp` was reading a playlist row's track under the
wrong key for every row on this account, so every Spotify playlist read back empty and the
exclusion guarantee silently covered Liked Songs only; and `songs_by_artist` returned
nothing with no explanation once `artist_top_tracks` was restricted (§6.6). Also filtered
Spotify's playlist listing to playlists the user owns, closing a second, related gap the
same live run surfaced. Both fixed and re-verified live on both backends before merging;
§7.1 carries the baseline.

**2026-09-10 — a number for the similarity path.** §7.2 closed: `quality_check.py
--similarity` scores `recommend_from_song` and `recommend_from_playlist` on signal
agreement (against a per-backend ceiling), artist concentration, cross-seed overlap and a
native-vs-graph A/B, with the noise floor measured in the same run. §6.5 landed again on
the first live run, which killed the A/B metric as originally designed — "displacement"
turned out to equal "additions" identically, because both arms truncate to `limit`;
replaced by churn plus a corroboration delta. The baseline it then produced moved §7.3 up
the list: 87% of Spotify's picks rest on a single signal, and same-artist seeds return 90%
the same songs there, neither of which more Deezer coverage can fix. It also corrected a
number this document had been quoting as universal — the 0.793 noise floor is YouTube's;
Spotify's is 1.00, because nothing upstream of the cached graph varies.

**2026-09-11 — the graph becomes plural.** §7.3 closed: `brainz.py` adds ListenBrainz
`similar-artists` as a second adjacency source behind MusicBrainz identity, tagged
`graph_related_lb` so a track both sources surface reads as agreement through the
`sources` set `signals._merge_and_score` already counts — the single-signal fix is the tag,
not new scoring. Reversed §7.3's own premise twice over: the "obvious pick" had already been
rejected in `graph.py`'s header, and that rejection had tested a track-level endpoint for an
artist-centric graph. Measured independent before being wired in (Jaccard 0.137 against
Deezer over eight seeds), which is the only thing that justified a second source at all.
The first live run found two defects, both now pinned by tests: a MusicBrainz 503 cached as
a permanent `no_match` — it had already silently emptied Arijit Singh — and the adjacency
seed taken from Deezer's credit, which names "Kesariya"'s composer rather than its singer
and returned unrelated neighbours. Also gave `graph.resolve_artist` the cache it always
needed, which stopped being optional once every ListenBrainz neighbour had to cross back
into Deezer by name. Deezer is still the catalogue; only adjacency is plural.

**2026-09-11 — the re-baseline, and a negative result.** §7.10: the second source did not do
what §7.3 built it for. Spotify's single-signal share went 87% → 84%, and the 90%
same-artist-seed overlap did not move at all. The cause is an error in §7.3's reasoning
rather than in the implementation — it justified the source on *independence* and expected
*corroboration*, which are opposites, and no artist-centric source can separate two seeds
that share an artist. What the source does deliver is variety: YouTube cross-seed overlap
0.042 → 0.029, same-artist 60/50% → 50/40%, Spotify concentration 0.272 → 0.24. Two costs
recorded rather than buried: Spotify's cross-seed overlap worsened (0.044 → 0.060) and
YouTube lost nine filled slots. The still-open defect gets §7.12: it needs a **track**-level
signal, a different kind of source rather than one more of the same kind.

**2026-09-11 — the lost slots, and two defects behind them.** §7.11: the ranked pool's depth
and the provider-search budget were one number, and §7.3's fourth source shifted the
native/graph mix far enough past the 1.6x buffer that `recommend_from_song` returned 8 songs
instead of 10. Split into `backfill_pool_size` (local, free) and `max_resolve` (the network
cost, unchanged) — the design `server.py` already described but only applied on the filtering
path. Two quieter defects fell out of it: `dropped` had been counting never-searched pool tail
as "couldn't be matched", telling the user 20 failures where 7 were real; and
`quality_check.py` carried its own copy of the pool sizing, so it went on reporting a short
result the fixed server no longer produced — §5's argument turned on the verification layer
itself. YouTube now fills 100/100 with 85 distinct songs, against §7.2's 82.

**2026-09-15 — corroboration stopped being uncountable across families.** §7.16: measured before
proposing, per its own plan. `scripts/measure_corroboration.py` (new, read-only) found 70 of 81
multi-member `_collapse_variants` clusters on YouTube mixed a native-keyed candidate with a
graph-keyed candidate for the same song — material, not a footnote. The fix was pure evidence
bookkeeping: `_collapse_variants` now unions `sources` and sums `score` onto the kept variant
instead of discarding the losers', while still picking *which* variant represents the cluster by
the old `(score, rank)` rule on pre-union numbers. Verified live, same account, before/after:
corroborated 0.59 → 0.87, both well clear of the 0.84–0.86 noise floor. The predicted cost landed
exactly as §7.15 foresaw — HHI 0.274 → 0.312, cross-seed overlap and distinct-song count barely
moved — and is the same tradeoff already bounded by `recommend_from_song`'s per-artist cap, not
re-litigated here. Left open: the Spotify prediction (near-zero effect, no native signals to
fragment against) couldn't be checked live in this environment for lack of configured
`spotify-mcp` credentials, and whether §7.12's same-artist defect narrows now that agreement
counts correctly.

**2026-09-16 — something above the pipeline decides.** §7.7 v0: `scripts/orchestrate.py`, a
Claude Agent SDK loop that spawns `server.py` as a stdio MCP subprocess and works a goal no
single call satisfies — 20 songs, no artist more than twice *across calls*, 15 genuine matches,
energy rising. Two consecutive live runs passed every constraint, each by a different route: the
agent probed `index_status`, tried a BPM-filtered query, read the shortfall out of the result's
own `notes` and `match_quality`, dropped the filter, fanned across energy tiers, and counted
artists itself. The task lost §7.7's 45-minute framing before any code existed, because durations
exist nowhere in re-com or either sibling server; count-based constraints kept v0 to one script
with zero engine changes. Two design choices did the real work: the checker reads songs as the
*tool* reported them, so a run cannot pass on the agent's say-so, and the context hook keeps
`notes`/`match_quality` verbatim while compacting song lists ~70% — trimming the signals the
agent replans on would have measured nothing. The one defect was in the harness (an MCP result
must go back as content blocks), and the agent met it by refusing to invent songs and reporting
the engine as broken.
