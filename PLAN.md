# re-com — Design & Build Plan

> Reference doc for future Claude agents working on this project. Read this before writing any code here.
> This file may reference `ytmusic-mcp` (a sibling project) for context on prior decisions — nothing else in this
> repo (README, code, comments) should, since re-com is meant to stand alone.

## Goal

Recommend genuinely **new** songs based on a seed song or seed playlist — better than a streaming service's built-in radio/autoplay, which the user has found mediocre and which resurfaces songs already liked. re-com improves on radio by combining multiple independent discovery signals instead of trusting one algorithm, and guarantees novelty by hard-filtering anything the user has already liked (or, for playlist seeds, anything already in that playlist).

re-com is meant to be a **general song-recommendation engine, not tied to one streaming service.** v1 is built entirely against YouTube Music (via `ytmusicapi`) since that's the account/library available today, but nothing about the goal, ranking approach, or tool contract is YouTube-specific — v3 (see below) plans to add Spotify as a second backend.

## Hard requirements (non-negotiable)

- Recommendations must **never** include a song already in Liked Music (playlist ID `LM`) **or already in ANY of the user's other playlists** — not just the one seeded from. (Originally scoped to Liked Music + seed playlist only; broadened after user feedback found songs already sitting in an unrelated playlist coming back as "new" recommendations, which defeats the point.)
- When seeded by a playlist, recommendations must **never** include a song already in that seed playlist either — even if it isn't liked. (Subsumed by the rule above for playlists in the user's library listing, but kept as an explicit belt-and-suspenders exclusion in `recommend_from_playlist` in case the seed playlist isn't one `get_library_playlists()` returns.)
- `recommend_from_song` must never return the seed song itself.
- v1 excludes BPM/tempo comparison entirely. YouTube Music exposes no tempo data. Revisit as a v2 stretch goal (needs a 3rd-party BPM source, e.g. GetSongBPM or AcousticBrainz — expect real coverage gaps).

## Why not just call YouTube's radio API directly

User feedback that shaped this design:
- YT Music's own radio/autoplay isn't good enough on its own.
- It resurfaces songs already in Liked Music, which defeats the purpose of a *discovery* tool.

Approach: treat radio as **one signal among several**, not the whole system, and always hard-filter out anything already liked.

## Architecture decision

- Standalone MCP server: **`re-com`**. Not a fork or extension of any other project — it has its own auth setup, its own scripts, its own README. (It happens to authenticate against the same YouTube account used elsewhere on this machine, but that's a user-level fact, not a code dependency — nothing in this repo imports or references another project.)
- Auth: same `ytmusicapi` browser-header approach as any `ytmusicapi`-based project — see this repo's own README for the exact steps. Auth file path is configurable via the `RECOM_AUTH_PATH` env var (default `headers_auth.json` in the project root), never hardcoded.
- **Read-only recommendation engine.** It does not create or modify playlists — it only returns ranked recommendations. Saving results into an actual YouTube Music playlist is a separate concern for whatever's orchestrating this tool (e.g. Claude calling a playlist-management MCP tool alongside this one).
- Own `handle_errors` wrapper: auth-expired / rate-limit-429 / network-error → clean, actionable messages, no raw tracebacks.

## Candidate generation — multi-signal design

For each seed song, pull candidates from 3 independent `ytmusicapi` sources:

1. **Radio** — `get_watch_playlist(videoId=seed, radio=True, limit=25)` — YT's own autoplay signal.
2. **Related** — the `related` browseId returned alongside the radio call, fed into `get_song_related(browseId)` — a separate "related content" signal, algorithmically distinct from radio. Any item with a `videoId` across any of its returned sections counts (not just the "You might also like" section).
3. **Artist expansion** — `get_artist(channelId)` for the seed's primary artist → that artist's own top songs, **plus** their top 2 related artists' top songs (one more `get_artist` call each). This goes deeper into an artist's catalog and adjacent artists than radio ever surfaces.

If any individual signal call fails (`YTMusicError` / network error), that signal is skipped for that seed rather than failing the whole recommendation — partial results beat no results.

For **playlist-seeded** recommendations: randomly sample up to `seed_sample_size` tracks from the playlist (default 5; use the whole playlist if it's smaller) as seeds, run the same 3-signal generation per seed track, then pool all candidates together. Sampling is random per call (not cached/deterministic) — re-running the same playlist recommendation is expected to surface different picks over time, which is a feature for a discovery tool, not a bug.

## Ranking

No ML needed for v1 — simple, explainable scoring:

- **Score = number of distinct (seed, source) pairs that surfaced a candidate.** Per seed, a candidate can be hit by at most 3 sources (radio, related, artist). A song surfaced by radio *and* related *and* multiple seed tracks (playlist mode) racks up a higher score — that's a strong convergence signal.
- Every returned recommendation carries a `sources` field (the union of which signal types ever surfaced it) and a `score`, so results are explainable, not a black box.

## Exclusion filter (applied last, always)

- `_library_video_ids()` builds the full exclusion set: Liked Music (`get_playlist("LM", limit=None)` — **not** `get_liked_songs()`, which defaults to only the most recent 100, not nearly enough for a library this size) **unioned with every track in every playlist** `get_library_playlists(limit=None)` lists (each fetched via `get_playlist(id, limit=None)`; a playlist that fails to fetch is skipped rather than failing the whole call, same partial-results philosophy as signal gathering). Used by all three tools (`recommend_from_song`, `recommend_from_playlist`, `songs_by_artist`).
- For playlist-seeded calls, the seed playlist's own track list is unioned in explicitly too, as a belt-and-suspenders exclusion in case that playlist isn't one `get_library_playlists()` happens to return.
- Dedupe candidates against each other (same videoId surfaced by multiple sources/seeds → one entry, sources unioned, score summed).

## Tools (v1)

- `recommend_from_song(video_id=None, song=None, artist=None, limit=20) -> list[{videoId, title, artists, album, score, sources}]`
  — `video_id` and `song`/`artist` are alternative ways to specify the seed; exactly one path must be given.
  When `song` is given instead of `video_id`, it's resolved to a videoId internally via `search(filter="songs")`
  (`_resolve_song_video_id`), preferring a result whose artist credit loosely matches `artist` if given, else the
  top search hit. Added because the user asked for "10 songs that relate to this song by this artist" to work
  without a separate lookup call first (previously an open question in this doc). Raises a clear
  `RuntimeError` if neither `video_id` nor `song` is given, or if `song`/`artist` matches nothing.
- `recommend_from_playlist(playlist_id, limit=20, seed_sample_size=5) -> list[{...}]`
- `songs_by_artist(artist, limit=10) -> {artist, requested, found, songs: [{videoId, title, artists, album}]}`
  — a deliberately different shape of tool from the two above. User-requested: "N songs by \[artist\]" is not
  a similarity recommendation, it's a direct catalog pull. Resolves the artist name via `search(filter="artists")`,
  pulls their real song catalog (`get_artist()`'s `songs.browseId` fed into `get_playlist` for full depth, falling
  back to the short `songs.results` preview if that lookup fails or there's no browseId), and excludes anything
  already in Liked Music **or in any of the user's playlists** — broader than `recommend_from_playlist`'s
  single-seed-playlist exclusion, since there's no single "seed playlist" here. No scoring/sources fields since
  there's no multi-signal ranking involved. Hard requirement (not best-effort): if fewer than `limit` qualifying
  songs exist after exclusion, returns however many were found — `found` vs `requested` tells the caller whether
  it fell short — rather than padding the list with worse substitutes. Never adds results anywhere (read-only,
  same as the rest of this server).
- *(v2, not in v1)* `compare_bpm(...)` — not implemented. Needs a 3rd-party tempo data source decision first.

## v2 — Mood-aware recommendation

Designed **and implemented** — see **`PLAN_V2.md`**, including its build status section. Summary of what it changes
relative to the open questions below:

- **BPM is demoted, not built.** Probing `get_song()` against the real account confirms YouTube Music
  exposes no tempo/key/audio features at all, and tempo is a poor mood proxy regardless. `PLAN_V2.md`
  replaces it with a mood-vector model fed by YT's own mood-playlist taxonomy, lyrics, and an LLM judge.
- **Mood/chart discovery as a 4th signal: yes** — `get_mood_categories()` returns 13 moods and
  `get_mood_playlists()` yields 2,223 playlists across them, which becomes a mood-labeled corpus
  (the "Mood Atlas"), not just an extra candidate source.
- **Genre-taxonomy matching: yes** — the hand-curated `C - *` playlists supply a per-track genre prior,
  used as a ranking term to stop cross-genre noise.
- **Rate-limit budget: addressed in Phase 0** — a SQLite-cached library snapshot removes the ~20s
  exclusion-set rebuild that currently runs on every single tool call.

## Open questions for v2 / future agents

> Superseded by `PLAN_V2.md`; kept for the reasoning trail.

- Which 3rd-party BPM/tempo API to use, and how to handle songs with no BPM coverage.
- Should ranking incorporate genre-taxonomy matching (the same idea used to bucket a liked-songs library into "Bollywood/Hindi", "Punjabi", genre playlists elsewhere) to avoid cross-genre noise in results?
- Rate-limit budget: playlist-seeded recs can trigger 25+ API calls per request (seeds × signals, including nested related-artist lookups). May need a call cap or caching layer if this proves slow or gets rate-limited in practice.
- Should mood/chart-based discovery (`get_mood_playlists`, `get_charts`) be added as a 4th signal for more diversity?

## v3 — Multi-provider support (Spotify)

**YouTube Music side done (2026-08-20).** `signals.py`/`server.py`'s live tool-call path no longer talks to `ytmusicapi` at all — `_client()` returns a `ytmusic_client.YTMusicClient`, a synchronous facade that spawns the sibling `ytmusic-mcp` server as an MCP subprocess and calls its tools (`search_music`, `get_playlists`, `get_playlist_tracks`, `get_watch_playlist`, `get_song_related`, `get_artist`, `get_history` — the last three were added to `ytmusic-mcp` in this session, it previously only exposed playlist-management tools). The facade exposes the exact method names/shapes `ytmusicapi.YTMusic` used to (`get_watch_playlist`, `get_playlist`, etc.), so `_gather_seed_candidates`, `_merge_and_score`, `_finalize`, `recommend.build`, `sense.read_mood` and everything downstream needed **zero changes** — the seam was already exactly `_client()`. re-com now holds no YouTube Music credential of any kind; `ytmusic-mcp` owns auth entirely. `RECOM_AUTH_PATH`/`headers_auth.json`/`setup_auth*.py` still exist but now only serve the **offline** maintenance scripts (`build_atlas.py`, `label_library.py`, `build_genres.py`, `snapshot_history.py`, `quality_check.py`), which are out of the live request path and were deliberately left untouched — see README's Setup section.

**Spotify side done (2026-08-20).** `spotify_client.SpotifyClient` implements the same interface `YTMusicClient` does, now formalized as `provider.Provider` (a `typing.Protocol`), and talks to a new sibling `spotify-mcp` server (spotipy-backed, purpose-built rather than adopting one of the several playback-focused third-party Spotify MCP servers already on GitHub — none matched the read-heavy discovery shape needed here). `_client()` picks a backend via `RECOM_PROVIDER` ("youtube", the default, or "spotify"); each backend is registered as a separate MCP server instance (`re-com` / `re-com-spotify`), per the tool-contract question below. Answers to the open questions, as decided:

- **Signal-shape matching: shaped spotify-mcp to fit, not a new contract.** `_gather_seed_candidates` and everything downstream are completely unchanged (zero-diff, same as the YouTube Music session) — `SpotifyClient` builds `get_watch_playlist`/`get_song_related`/`get_artist`-shaped responses out of Spotify's actual endpoints (seed-track `/recommendations`, related-artists' top tracks, the seed artist's own top tracks) rather than growing a backend-agnostic seed-signal contract that would have meant rewriting the proven candidate-generation logic and its whole test suite. `provider.py` documents this choice and formalizes the resulting interface as a `Protocol`.
- **Tool contract: separate MCP server instance**, not a `provider` argument — `re-com-spotify` alongside `re-com`, exactly as this section anticipated.
- **IDs: still not renamed**, deliberately, same reasoning as before — `videoId` now holds a Spotify track id for Spotify-provider instances. Revisit if a third provider or a cross-provider use case ever makes the YouTube-specific name actively confusing.
- **Signal parity confirmed uneven, handled by graceful degradation.** Spotify restricts `/recommendations` and `artist_related_artists` for API apps created after Nov 2024 without Extended Quota Mode. `SpotifyClient` treats a 403 on either exactly like YouTube Music treats a failed signal — skip it, keep going — rather than failing the whole seed. `get_artist`'s top-tracks call is also just thinner than YouTube Music's full-catalog playlist (Spotify caps it at ~10); documented in `spotify_client.py` and both READMEs rather than silently under-delivering.
- **Mood engine (v2) is YouTube-only.** `recommend_for_mood`/`recommend_from_playlist_for_mood`/`read_my_mood` depend on `atlas.py`'s YouTube mood-playlist crawl, which has no Spotify equivalent yet — building one (Spotify's audio-features endpoint, ironically, would make a *native* mood signal possible where YouTube Music needed the whole atlas+lyrics+LLM stack to fake one) is out of scope for this session. `recommend_from_song`, `recommend_from_playlist`, `songs_by_artist`, and `refresh_library` work on both backends.

Open design questions from before this session, kept for the reasoning trail:

- **Provider abstraction, generalized.** The YouTube Music work above is the concrete instance of the pattern: a `Provider` should be "an MCP-client facade over a sibling `*-mcp` server that owns that backend's auth," not a bespoke Python class re-com authenticates itself. A `SpotifyProvider` should spawn/talk to a future `spotify-mcp` server the same way `YTMusicClient` talks to `ytmusic-mcp` — re-com stays credential-free for Spotify too. `ytmusic_client.YTMusicClient` is the reference shape to copy (same connect/call/close plumbing, different tool names and unwrap logic).
- **Signal-shape matching is real work per provider.** `YTMusicClient`'s methods intentionally mirror `ytmusicapi.YTMusic`'s exact signatures/return shapes so the rest of re-com didn't need to change. A `SpotifyProvider` won't have that luxury — Spotify's actual endpoints (top-tracks, related-artists, recommendations) don't line up 1:1 with YouTube Music's (radio, related, artist expansion), so either `spotify-mcp`'s tools need to be shaped to fit `_gather_seed_candidates`'s expectations, or `_gather_seed_candidates` needs to grow a real `Provider` protocol with a shared, backend-agnostic seed-signal contract instead of assuming `ytmusicapi`-shaped methods. Worth deciding which before writing `spotify-mcp`.
- **Auth is a bigger difference than it looks, but it's now someone else's problem.** YouTube Music auth is a copy-pasted browser header; Spotify uses real OAuth (client ID/secret, redirect URI, refresh token) via the Spotify Web API. Since a future `spotify-mcp` would own that entirely (mirroring `ytmusic-mcp`), re-com itself doesn't need to care which flow it is — but `spotify-mcp` will need its own `scripts/setup_auth_spotify.py`-equivalent.
- **Signal parity isn't guaranteed.** Spotify's Web API has artist top-tracks and (historically) a recommendations/related-artists endpoint, but Spotify has been actively deprecating/restricting several discovery endpoints for newer app registrations — check current API access levels before assuming parity with the YouTube Music 3-signal design above.
- **Tool contract question:** does `recommend_from_song`/`recommend_from_playlist` gain a `provider` argument, or does provider selection happen at the MCP-server-instance level (e.g. a separately configured `re-com-spotify` server)? Whichever it is, a single call should almost certainly stay within one provider — cross-provider merging (e.g. seeding from a YouTube Music playlist but recommending Spotify tracks) is out of scope unless a future agent has a concrete reason to want it.
- **IDs are provider-specific.** `video_id` is currently baked into the tool signatures and output (`videoId` field) as YouTube Music terminology. This session deliberately left that rename undone (see git history/PLAN discussion around 2026-08-19) rather than doing it speculatively — but it's the first thing to revisit once a second provider actually exists, since Spotify track IDs/URIs aren't YouTube video IDs.

## v6 — Provider-agnostic engine on a neutral music graph

**The goal changed (user, 2026-08-23):** re-com is to be a recommendation *app* — whichever service the
user connects (YouTube Music, Spotify, others later), it takes that library and applies the same logic.
v3 made the *backend* swappable; it did not make the *engine* portable, and measuring why is what this
section records.

### What forced the redesign: Spotify's API is gutted

Measured directly against the real app registration (2026-08-23), not inferred from docs:

| Works | Dead |
| --- | --- |
| saved tracks, playlists, recently played, top tracks/artists | `/recommendations` (404) |
| `search` (tracks, artists, playlists) | `artist_related_artists` (403) |
| `track`, `artist`, `artist_albums` → `album_tracks` | `artist_top_tracks` (403) |
| | `audio_features` / `audio_analysis` (403) |
| | reading **any other user's playlist** (403) |
| | `categories`, `featured_playlists`, `new_releases` (403) |

This is the post-Nov-2024 restriction for app registrations without Extended Quota Mode. Consequences:

- **Two of three signals are unbuildable on Spotify.** Only artist expansion survives, and only via
  `artist_albums` → `album_tracks` (which is actually *deeper* than the 10-track `artist_top_tracks` cap
  it replaces — but note `artist_albums` 400s above `limit=10` on a restricted app).
- **Playlist co-occurrence and a Spotify mood atlas are both impossible**, because playlists can be
  *found* but not *read*. Both were probed and both 403.
- **`audio_features` is gone**, killing the one path that would have made a native Spotify mood signal
  easier than YouTube's atlas+lyrics+LLM stack. PLAN's v3 note anticipating that is now obsolete.
- Live consequence: `recommend_from_song` on Spotify returns **0 songs**.

**The general lesson, which is the actual architectural point:** provider discovery endpoints cannot carry
a provider-agnostic engine. They differ wildly, they get revoked unilaterally, and YouTube Music has no
official API at all. Anything built on them breaks per-provider — exactly the problem this version exists
to solve.

### The three-layer split

| Layer | Owns | Swappable? |
| --- | --- | --- |
| **Provider** (`ytmusic-mcp`, `spotify-mcp`, …) | identity, library, listening history, playlist writes | yes — this is the only per-service part |
| **Music graph** (Deezer) | similarity, artist adjacency, mood corpus, tempo | one implementation, service-neutral |
| **Local** (`store.py`, `recommend.py`, `arc.py`, `label.py`, `judge.py`) | mood vectors, arcs, ranking, feedback, exclusion | already portable |

The provider supplies *whose taste this is*; the graph supplies *what sounds like what*. Recommendation
quality stops depending on any one service's API politics.

### Why Deezer is the graph (measured, not assumed)

Two candidates were probed live. **ListenBrainz/MusicBrainz was rejected on evidence:**

- MusicBrainz *identity resolution* is excellent — 7/8 test tracks, every Punjabi/Bollywood one at
  score 100. Worth remembering if a second graph is ever needed.
- But ListenBrainz's `similar-recordings` returned **empty for all 6 resolved tracks**, including
  *Blinding Lights*, across every algorithm variant its API accepts. Calls took **12–19s** each, and
  MusicBrainz 503s under even 1 req/sec. Unusable on a live path; would need their bulk dumps imported
  offline, which is its own project.

**Deezer wins on every axis that matters here**, and re-com already talks to it (`tempo.py`):

| Capability | Endpoint | Measured |
| --- | --- | --- |
| Track identity | `/search?q=artist:"X" track:"Y"` | **6/6**, ~0.9s |
| Related artists | `/artist/{id}/related` | **6/6** |
| Artist catalogue | `/artist/{id}/top`, albums | works |
| Artist radio | `/artist/{id}/radio` | **uneven** — 25 tracks for The Weeknd, **0 for AP Dhillon** |
| **Playlist search *and read*** | `/search/playlist`, `/playlist/{id}/tracks` | **works** |
| BPM | already built | 36% coverage |

No API key, no auth, no attribution requirement — the same reasons it was chosen for tempo.

**The related artists are culturally correct, which is the thing that actually matters for this library:**
AP Dhillon → Diljit Dosanjh, Shubh, Garry Sandhu, Karan Aujla, Amrinder Gill. Sidhu Moose Wala → Prem
Dhillon, Cheema Y, Karan Aujla. This is the Punjabi/Bollywood catalogue that Deezer's *tempo* index only
reached 6–16% of — **resolution and adjacency are far better covered than BPM**, so the tempo coverage
table is not a proxy for graph coverage. Do not conflate them.

**The mood atlas becomes provider-neutral.** Deezer allows exactly what Spotify forbids — searching
playlists *and reading their tracks* — so `atlas.py`'s core idea (learn track↔mood from playlist
membership) rebuilds on Deezer for any backend. Probed `'punjabi sad'` and `'bollywood romantic'` and both
returned readable playlists, i.e. it reaches the catalogue YouTube's English-centric mood playlists
barely touched (the 4.1% coverage problem `PLAN_V2.md` documents).

### Known costs, stated up front

- **No track-level radio.** Deezer has no `/track/{id}/radio` or `/track/{id}/related` (both probed,
  `InvalidQueryException`). YouTube's per-track radio is re-com's single strongest signal and it has no
  Deezer equivalent — similarity becomes **artist-centric**. This is a genuine quality regression on
  YouTube and must be measured with `scripts/quality_check.py`, not assumed away. Keeping YouTube's
  native radio as a provider-optional *bonus* signal (see below) is how to avoid paying it there.
- **Artist radio is uneven**, empty for exactly the kind of artist this library is full of. Treat it as a
  best-effort fourth signal, never a required one.
- **The round trip is the new cost.** The graph returns "Diljit Dosanjh — Born to Shine"; the provider
  needs its own ID to return it, which is ~1 search per candidate. **Resolve lazily** — rank on
  graph-side metadata, then resolve only the final N (~20–30), parallelised through the
  `signals.gather_seeds` pool built on 2026-08-23. Resolving a 500-candidate pool eagerly would be
  absurd. A candidate that fails to resolve on the provider is dropped with a note, same
  partial-results philosophy as everywhere else.
- **Matching is fuzzy and will be wrong sometimes.** The bridge is title+artist normalisation —
  `signals.same_song`/`_song_key` and `tempo.py`'s resolver already do this and should be extracted into
  one shared module rather than a third copy.

### Design decisions to make before building

- **Signals become capability-gated strategies**, each declaring what it needs, with the engine running
  whatever is available and ranking on agreement — which is what `_merge_and_score` already does. A
  provider advertising native radio (YouTube) contributes it; one that can't (Spotify) simply has fewer
  sources agreeing, rather than returning nothing. Graceful degradation is already the house style.
- **The store needs a provider dimension.** Today both instances share `~/.recom/store.db` with no
  provider column, and nothing gates the mood tools by backend — so `recommend_for_mood` on the Spotify
  instance silently returns **YouTube videoIds**, having sent YouTube ids to Spotify (`400 Invalid base62
  id` on every seed) and fallen back to atlas candidates. The exclusion guarantee is meaningless there
  too, since the exclusion set is YouTube-namespaced. **This is a live correctness bug, not a
  future concern** — fix it first, before any of the above.
  Options: a `provider` column on track-keyed tables (composite keys; enables one taste profile across
  services later) versus a DB per provider (trivial, matches v3's "separate server instance" decision,
  but fragments a user who connects both). Leaning toward the column, since an *app* wants one identity.
- **Graph-side caching is mandatory.** Deezer resolution is permanent and already cached for tempo;
  related-artists and playlist membership should live in the same SQLite store on the same principle
  (cache the negative results too — `tempo.py`'s lesson).
- **Offline scripts must move onto the Provider interface.** `build_atlas.py`, `label_library.py`,
  `build_genres.py`, `snapshot_history.py` all still `import ytmusicapi` directly, which is why
  `history_log` — and therefore the whole implicit-feedback loop added 2026-08-23 — is YouTube-only in
  practice. Provider-agnostic recommendations need provider-agnostic indexing.

### What is already portable and should not be rebuilt

Worth stating so a future agent doesn't over-scope: the **lyrics + Claude judge** layer works off
title/artist and is already service-neutral; **artist mood propagation** is pure local computation and was
the single largest coverage contributor on YouTube (553 of 1,033 labelled songs); the **arc sequencer**,
**mood vector space**, **fluff cap**, **feedback loop** and **exclusion machinery** never touched a
provider API. The portable share of the mood engine is much larger than the atlas dependency suggests.

## v4 — Respect native YouTube Music dislikes

Not started. User-requested (2026-08-19): never recommend a song the user has thumbs-downed on YouTube
Music, the same way Liked Music is already a hard exclusion.

- **No bulk API for this.** `ytmusicapi`'s `YTMusic` client has `get_liked_songs()` but no
  `get_disliked_songs()` counterpart — confirmed by enumerating its public methods. A song's `likeStatus`
  (`LIKE` / `DISLIKE` / `INDIFFERENT`) is only exposed per-song (via `get_watch_playlist`/`get_song`) or
  inside `get_history()`'s most recent 200 items. There is no single call that returns "every disliked
  song," unlike the `LM` playlist ID that makes Liked Music a one-shot fetch.
- **Implication: this has to be a persistent log, not a snapshot fetch.** The exclusion set can't be
  rebuilt fresh each call the way `_library_video_ids` is. Likely shape: extend
  `scripts/snapshot_history.py` (already run on a cron for the mood-sensing timeline) to read `likeStatus`
  off each history item and upsert `DISLIKE` rows into a new `store.py` table or into `feedback` with
  `source="native_dislike"` — reusing the existing `rejected_video_ids()`-style exclusion machinery in
  `recommend.py` rather than inventing a second filter path.
- **Coverage will be partial and grows only over time.** A song disliked once but never appearing again in
  a 200-item history window would never be observed. Worth stating plainly in the tool/README docs rather
  than implying a complete guarantee the way the Liked Music exclusion can.
- Deliberately kept out of v2/v3 scope — it's an orthogonal exclusion concern, not mood or multi-provider
  work, and the no-bulk-API constraint makes it a real design task rather than a quick addition.

## v5 — Movies & TV recommendations

Not started. User-requested (2026-08-23): extend re-com beyond music into a second **domain**, not just a
second provider — recommend movies/TV shows the same way it recommends songs (multi-signal, explainable,
never something already watched).

**This is a bigger fork than v3 was.** v3 added a second *provider* inside the same domain (songs) and the
whole point of that work was that `_gather_seed_candidates`, `recommend.py`, `signals.py` needed **zero
changes** — only `_client()`'s target and the shape-translation layer (`spotify_client.py`) changed. Movies
and TV are not a second music provider; there is no single sibling `*-mcp` server that plays the
`ytmusic-mcp`/`spotify-mcp` role (owns auth, exposes a personal watched/watchlist history, exposes
catalog + related-content signals) the way both music backends do. That server would have to be built
first, and *which one* is the first open question below rather than a settled decision — unlike v3, where
Spotify was the obvious second backend from the start.

### Open question 1: what backend plays `ytmusic-mcp`'s role

Three real candidates, each with a different gap relative to what music had for free:

- **Trakt.tv** — closest structural match. Has scrobble/watch-history and a watchlist model that map
  onto "Liked Music" + playlists fairly directly, plus `related` and `recommendations` endpoints that
  could stand in for radio/related/artist-expansion. Would need a new sibling `trakt-mcp` server, built
  the same way `ytmusic-mcp` and `spotify-mcp` were — a purpose-built MCP server that owns Trakt OAuth
  and exposes tools shaped for this use case, not one of whatever generic Trakt MCP servers may already
  exist on GitHub (same reasoning `spotify-mcp` used: none matched the read-heavy discovery shape needed
  here, so building fit better than adapting).
- **TMDb** — the deepest catalog and genuinely good `/recommendations` and `/similar` endpoints per
  title, no OAuth needed for read-only catalog data. The gap: **no personal watch history at all.**
  TMDb has no concept of "what this account has already watched" — that would have to come from
  somewhere else entirely (a Trakt account linked for history only, or a locally-maintained watched log
  the same shape `store.py` already keeps for music feedback/history). Strong candidate for the
  *catalog + signals* half of the job, weak-to-absent for the *exclusion guarantee* half — and the
  exclusion guarantee is re-com's whole reason to exist, per the Goal section above.
- **Letterboxd** — has real personal watched/watchlist/diary data, which is the piece TMDb lacks, and is
  the natural fit for *films* specifically (it has no TV support at all, which alone rules it out as a
  sole backend for this v5 scope). No official public API, though — anything built against it means
  scraping the user's own profile pages, a materially less stable foundation than an OAuth'd REST API,
  and the kind of dependency the `ytmusic-mcp`/`spotify-mcp` pattern was explicitly designed to avoid
  (both of those talk to real, documented APIs, however restricted).

**No decision made.** A plausible shape given the above: TMDb for catalog/related/similar signals (the
`radio`/`related`/`artist`-equivalent multi-signal pool) plus Trakt for watched-history and watchlist
(the exclusion set) — i.e. two data sources behind one `Provider`, mirroring how the music side's mood
engine (v2) already pulls YouTube's atlas, lyrics, and artist propagation into one label rather than
trusting a single source. That would still mean building a `trakt-mcp` (or reusing Trakt's own official-ish
OAuth flow directly, if a first-party auth story turns out to be simple enough not to need a sibling
server) before anything else here can start.

### What would carry over from the music architecture, and what wouldn't

**Carries over cleanly:**
- The **hard exclusion guarantee** as the organizing principle — never recommend something already in
  the equivalent of Liked Music/a playlist, here: already marked watched, or already on a watchlist.
- The **multi-signal, explainable ranking** design (`_merge_and_score`'s "how many independent signals
  agree" scoring) — genuinely domain-agnostic; nothing about counting `(seed, source)` hits assumes music.
- The **`Provider` protocol pattern** from `provider.py` — "an MCP-client facade over a sibling `*-mcp`
  server that owns that backend's auth" is exactly the shape a `trakt_client.py`/`tmdb_client.py` would
  take, including re-com holding zero credentials of its own for it.
- **Read-only, always.** Same reasoning as the music side's "a recommendation engine that also mutates
  the library can't be trusted to have excluded what it just added" — this server would never mark
  something watched or touch a watchlist itself.
- The **mood-vector idea (v2)** is arguably a *better* fit for film/TV than it was for music — mood-based
  "what should I watch tonight" is a well-worn category (Letterboxd lists, Trakt's own "mood" browsing
  attempts) and TMDb's keyword/genre metadata is richer and more consistent than YouTube Music's
  genre-page scraping ever was. Worth treating as a v6-shaped stretch once v5's core exists, not folding
  into v5 itself.

**Does not carry over / needs real rework:**
- **`_gather_seed_candidates`'s 3-signal shape is music-specific in substance, not just naming.** Radio
  (autoplay-style continuation) and artist-expansion (an artist's own catalog) don't have obvious 1:1
  film/TV analogues — there's no "director expansion" signal with the same density a music artist's
  back-catalog has, and "similar title" (TMDb `/similar`, Trakt `related`) is closer to the `related`
  signal alone. A 3-signal design here likely looks more like: similar-title, recommendations-for-title,
  and cast/director/franchise expansion — needs its own design pass, not a copy of `signals.py`.
  Realistically this is closer to `_gather_seed_candidates` needing a genuine rewrite than the
  zero-diff `Provider`-shape-matching trick v3 pulled off for Spotify.
- **No "seed_sample_size from a playlist" equivalent is obvious.** `recommend_from_playlist` samples
  tracks from a music playlist as seeds; the closest analogue (sampling from a Trakt list or Letterboxd
  list) is plausible but untested — lists on those services are usually curated/thematic in a way
  playlists sometimes aren't, so random sampling might behave differently.
- **`videoId` as the id field name is even more actively wrong here than it was for Spotify** (see v3's
  closing note on this). A movies/TV domain is exactly the "third provider" case that note said would
  force the rename — `videoId` cannot reasonably hold a TMDb/Trakt id. This is also the point where
  re-com stops being cleanly "one domain, several providers" and needs an explicit `media_type`
  (`song` / `movie` / `show`) concept threaded through tool responses, since a single MCP server
  instance recommending both would need to disambiguate what a returned id actually points at.
- **Separate server instance, same pattern as `re-com-spotify`.** This should almost certainly be a new
  `re-com-movies` (or similar) MCP server registration, not a mode flag on the existing one — same
  reasoning as v3's tool-contract answer: keep one call inside one domain, don't merge cross-domain
  ranking into a single response shape that has to explain itself to a caller.

### Honest scope note

This is closer to standing up a sibling project (`re-com-movies`, its own `trakt-mcp`/`tmdb-mcp`, its own
`Provider` implementation, its own signal design) than extending the current one. Reasonable first slice,
if this gets picked up: build the sibling auth server and a single `recommend_from_title` tool against
TMDb's `/similar` + `/recommendations` alone, with **no** watched-history exclusion yet (explicitly
documented as a known gap, the way v1's music side never had unexpected silent gaps) — get the
one-signal-first version working end-to-end before deciding whether Trakt is worth the second OAuth
integration just for the exclusion guarantee.

## Build status

**Done:**
- Full scaffold in place: `pyproject.toml`, `.gitignore`, `LICENSE`, `README.md`, `scripts/setup_auth.py`, `scripts/setup_auth_from_file.py`, `scripts/test_recommend.py`.
- `server.py` implements both v1 tools (`recommend_from_song`, `recommend_from_playlist`) exactly per the design above, plus the shared `handle_errors` wrapper.
- `.venv` created, dependencies installed (`pip install -e .`), and `headers_auth.json` is in place (copied from an already-authenticated `ytmusicapi` session on this machine — no browser-auth flow needed to get running).
- Fixed one real bug found during smoke testing: `get_song_related` sections can contain plain strings (e.g. artist bio text) instead of track dicts, which crashed related-content parsing. Fixed with an `isinstance(item, dict)` guard in `_gather_seed_candidates`.
- **Verified working end-to-end against real account data:**
  - `recommend_from_song` seeded on Daft Punk's "One More Time" → 10 sensible recommendations, zero overlap with Liked Music.
  - `recommend_from_playlist` seeded on a real playlist → 15 recommendations, zero overlap with Liked Music or the seed playlist.

**Not done yet:**
- v2 items below are all still open.

**Since the above was written:**
- Git repo initialized, pushed to `github.com/umsachde/re-com` (`origin/main`).
- Registered with Claude Code via `claude mcp add re-com -s user ...` — connected.
- Added a real unit test suite (`tests/test_server.py`, 34 tests) against a fake YTMusic client — covers `_norm_track`, `_merge_and_score`, `_finalize`, `_gather_seed_candidates` (including the isinstance-guard regression and the related-artist expansion cap), `_liked_video_ids`, `handle_errors`, and both tools end-to-end. No network/auth needed; run with `pytest`.
- Found and fixed a real bug this way: `handle_errors` caught `YTMusicServerError` before `YTMusicGatedError`, and since the latter subclasses the former, the gated-content branch was dead code — gated errors always fell through to the generic "server error" message instead of the clearer gated-specific one. Reordered the `except` clauses (subclass before superclass) to fix.

**Aside (does not affect re-com, but happened during this build):** a `remove_from_playlist` tool was added to the separate `ytmusic-mcp` project to clean up a duplicate-track bug found in a "C - Country" playlist while smoke-testing `recommend_from_playlist` against real data. That's `ytmusic-mcp` maintenance, unrelated to re-com's own scope — mentioned here only so a future agent doesn't wonder why an unrelated commit landed mid-build.

**2026-08-19 session — three follow-up features/fixes driven directly by user feedback, each committed separately:**
- Added `songs_by_artist` — see its writeup under Tools (v1) above. New helpers: `_library_video_ids`, `_resolve_artist`, `_artist_song_catalog`.
- Fixed a real exclusion bug: `recommend_from_song`/`recommend_from_playlist` only ever filtered against Liked Music (+ seed playlist for the latter) — a song already sitting in some *other* playlist could still come back as a "new" recommendation. Both now use `_library_video_ids` (the helper built for `songs_by_artist`), so all three tools share one exclusion definition. Verified against the real account before and after (same seed produced overlapping results pre-fix, zero overlap post-fix).
- Added seed-by-search to `recommend_from_song` (`song`/`artist` params, new `_resolve_song_video_id` helper) — resolves the "accept a search query" open question that used to be listed below.
- Test suite grew from 34 → 57 tests alongside these changes (one test per new code path, not just happy-path coverage — signal-failure branches, shortfall/no-match/validation-error cases, and the belt-and-suspenders seed-playlist exclusion are all explicitly covered). Also closed 3 pre-existing gaps in `_gather_seed_candidates` (seed-artist lookup failure, a related-artist with no browseId, a related-artist lookup failure) found while auditing coverage.
- `pytest-cov` added as a dev dependency; `server.py` line coverage is 98% (220 statements, 4 missed — `_client()`'s real YTMusic() construction and the `if __name__ == "__main__"` entrypoint, neither meaningfully unit-testable without a live auth session / actually running the server as a process). Run `pytest --cov=server --cov-report=term-missing` to reproduce.
- README and this file were updated in the same commits as each change — no doc lagging behind code at end of session.

**2026-08-19 (later session) — v2 Phase 0, first slice: library exclusion cache.**
- Every tool call rebuilt the exclusion set from scratch: Liked Music (~1,100 tracks, 7.4s) plus all 28
  playlists (~1,550 tracks, 12.0s). Measured at **20.5s of pure overhead per call.**
- Now cached to `~/.recom/library_cache.json` (~22 KB), TTL 6h, both configurable via
  `RECOM_CACHE_PATH` / `RECOM_CACHE_TTL` (`0` disables caching).
- **The novelty guarantee is preserved for likes, not just deferred.** A cache hit re-fetches only the
  most recent page of Liked Music (`limit=100`, ~1.3s) and unions it in — measured because newly liked
  songs land at the top of `LM`, so a bounded fetch catches them. The residual gap is a song added to a
  *different* playlist within the TTL; `refresh_library()` (new tool) forces a rebuild for that, and the
  three existing tools' docstrings point at it so an orchestrating agent knows to call it after writes.
- Top-up failure degrades to the cached set rather than failing the call — same partial-results
  philosophy already used for discovery signals.
- Measured end-to-end on the real account: exclusion set 20.5s → 0.9s; `recommend_from_song` ~24s → 4.3s;
  `songs_by_artist` ~22s → 2.6s.
- Tests 60 → 77. New `conftest.py` autouse fixture redirects `CACHE_PATH` to a temp file per test, so the
  suite can never read or write the developer's real cache. Coverage of the new code is complete.
- Known pre-existing coverage gaps, untouched by this change: two branches in `_artist_names_match` /
  `_filter_same_artist`, both introduced with `same_artist_only` in commit 128878f.

**2026-08-20 session — v3, YouTube Music side: re-com no longer holds any YouTube Music credential.**
- User-requested: re-com should rely on the sibling `ytmusic-mcp` server for everything YouTube-Music-related
  and know nothing about YouTube login. New `ytmusic_client.py`: a synchronous facade (`YTMusicClient`) that
  spawns `ytmusic-mcp` as an MCP subprocess (stdio) and exposes the same method names/shapes
  `ytmusicapi.YTMusic` used to (`search`, `get_playlist`, `get_library_playlists`, `get_watch_playlist`,
  `get_song_related`, `get_artist`, `get_history`) so every caller downstream of `_client()` needed zero
  changes.
- `ytmusic-mcp` gained three new tools it didn't have (`get_watch_playlist`, `get_song_related`, `get_artist`)
  plus a `limit` param on `get_playlists` (previously hardcoded to ytmusicapi's default of 25, which silently
  truncated the library-cache rebuild). 30 tests there, all passing.
- `server.py`: removed `ytmusicapi`/`YTMusicError` imports, `AUTH_PATH`/`AUTH_HELP`, and all the
  auth/JSON-decode/HTTP-401/403/429-specific branches from `handle_errors` — `ytmusic-mcp` already translates
  those into one clear `YTMusicMCPError`, so `handle_errors` just passes its message through. `signals.py`'s
  `_SIGNAL_ERRORS` narrowed from `(YTMusicError, requests.exceptions.RequestException)` to
  `(YTMusicMCPError,)` for the same reason.
- Verified live end-to-end against the real account through the new path: `recommend_from_song`,
  `recommend_for_mood`, and `scripts/test_recommend.py` (rewritten to talk to `ytmusic-mcp` instead of opening
  `headers_auth.json` itself) all work.
- **Deliberately out of scope for this session:** the *offline* maintenance scripts (`build_atlas.py`,
  `label_library.py`, `build_genres.py`, `build_tempo.py`, `snapshot_history.py`, `quality_check.py`,
  `setup_auth.py`/`setup_auth_from_file.py`) still construct `ytmusicapi.YTMusic(RECOM_AUTH_PATH)` directly.
  They're CLI tools the user runs themselves, not part of the live tool-call path, and converting them would
  mean routing bulk/one-off indexing operations through individual MCP tool calls for no live-request benefit.
  `atlas.py`, `lyrics.py`, `taxonomy.py`, `judge.py`, `label.py` (the modules those scripts call into) are
  untouched and still import `ytmusicapi.exceptions` directly for that reason.
- Tests: 288 passing (server.py 93% line coverage). `tests/test_server.py`'s `handle_errors` tests rewritten
  for the simplified version; its `_FakeYT`/signal-failure tests now raise `YTMusicMCPError` instead of
  `ytmusicapi`'s exception types. Two tests in `tests/test_v2.py` (`recommend.build`/`recommend.bridge_expand`
  surviving a dead seed) updated the same way; everything else in `test_v2.py` that raises `YTMusicError`
  tests `atlas.py`/`lyrics.py`/`label.py` directly and was untouched since those modules didn't change.
- README and this file updated in the same session.
