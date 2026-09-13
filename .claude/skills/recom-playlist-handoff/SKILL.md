---
name: recom-playlist-handoff
description: Turn a re-com recommendation into a real playlist on the backend (YouTube Music or Spotify). Use whenever the user wants recommended/mood songs saved, added to a playlist, or turned into a playlist — re-com itself never writes anything.
---

re-com is deliberately **read-only** (see `PLAN.md` §4.9) — it never creates a
playlist or adds a track anywhere, so "recommend me songs and make it a
playlist" is always three separate steps, in this exact order. Skipping the
third step is the footgun: the cached library-exclusion set stays stale for up
to `RECOM_CACHE_TTL` (default 6 hours), so a later recommendation call can hand
back a song you just saved as if it were still "new".

## The three steps

1. **Get the songs** from re-com, not from a bare `ytmusic`/`spotify` search:
   - `recommend_from_song`, `recommend_from_playlist`, `songs_by_artist`,
     `recommend_for_mood`, or `recommend_from_playlist_for_mood`.
   - Never hand-pick candidates from the raw `ytmusic`/`spotify` MCP search
     tools instead — that bypasses re-com's library-exclusion guarantee
     entirely, and a search result's own "already in library" flag is not
     reliable.

2. **Create/add via the playlist-management MCP server for that backend** —
   `ytmusic` (`create_playlist` / `add_to_playlist`) or the equivalent Spotify
   tools — using the ids (`videoId` for YouTube, track id for Spotify) that
   re-com's response returned. re-com has no playlist-write tool of its own;
   don't look for one.

3. **Call `refresh_library()` on the same re-com instance immediately after.**
   This is not optional cleanup — without it, the next recommendation call in
   this session (or within the TTL) can recommend something you just added.
   Call it once per batch of adds, right after the add call succeeds, not at
   the end of an unrelated later turn.

## Which re-com instance to refresh

There are two separate re-com registrations, one per backend
(`re-com` for YouTube Music, `re-com-spotify` for Spotify), each with its own
store and library cache (`PLAN.md` §4.10). Call `refresh_library()` on the
**same** instance whose backend you just wrote to — refreshing the other one
does nothing for the cache that matters.

## Quick check if something looks wrong

If a recommendation right after a save still includes a song you just added,
the most likely cause is a missed or wrong-instance `refresh_library()` call,
not a bug in the recommendation logic itself.
