"""Unit tests for spotify_client.py's shape-translation logic.

Everything here runs against a hand-rolled fake `_call` -- no network, no
Spotify credentials, no spotify-mcp subprocess required. This complements
(does not replace) a real-account smoke test against spotify-mcp.
"""

import pytest

from spotify_client import SpotifyClient, SpotifyMCPError, _artist_from_spotify, _track_from_spotify


def _client(calls: dict):
    """A SpotifyClient with its subprocess plumbing skipped entirely --
    _call is replaced by a fake that looks up canned responses (or raises)
    by tool name."""
    client = SpotifyClient.__new__(SpotifyClient)

    def fake_call(tool, *, unwrap=True, **arguments):
        entry = calls.get(tool)
        if entry is None:
            raise AssertionError(f"unexpected call to {tool}({arguments})")
        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            return entry(**arguments)
        return entry

    client._call = fake_call
    return client


# --- _track_from_spotify / _artist_from_spotify -----------------------------


def test_track_from_spotify_basic():
    item = {
        "id": "t1",
        "name": "Song",
        "artists": [{"name": "A", "id": "a1"}, {"name": "B", "id": "a2"}],
        "album": {"name": "Album Name"},
    }
    norm = _track_from_spotify(item)
    assert norm == {
        "videoId": "t1",
        "title": "Song",
        "artists": [{"name": "A", "id": "a1"}, {"name": "B", "id": "a2"}],
        "album": {"name": "Album Name"},
    }


def test_track_from_spotify_drops_unnamed_artists():
    item = {"id": "t1", "name": "Song", "artists": [{"id": "a1"}]}
    assert _track_from_spotify(item)["artists"] == []


def test_track_from_spotify_album_string():
    item = {"id": "t1", "name": "Song", "album": "Album Name"}
    assert _track_from_spotify(item)["album"] == {"name": "Album Name"}


def test_track_from_spotify_no_album():
    item = {"id": "t1", "name": "Song"}
    assert _track_from_spotify(item)["album"] is None


def test_track_from_spotify_empty_item():
    assert _track_from_spotify(None) == {}
    assert _track_from_spotify({}) == {}


def test_artist_from_spotify():
    assert _artist_from_spotify({"id": "art1", "name": "Some Artist"}) == {
        "artist": "Some Artist",
        "browseId": "art1",
    }


def test_artist_from_spotify_empty():
    assert _artist_from_spotify(None) == {}


# --- search ------------------------------------------------------------


def test_search_tracks_default():
    yt = _client({"search_music": [{"id": "t1", "name": "Song", "artists": []}]})
    results = yt.search("song")
    assert results == [{"videoId": "t1", "title": "Song", "artists": [], "album": None}]


def test_search_artists_filter():
    calls = {}

    def fake_search_music(query, filter, limit):
        calls["filter"] = filter
        return [{"id": "a1", "name": "Some Artist"}]

    yt = _client({"search_music": fake_search_music})
    results = yt.search("some artist", filter="artists", limit=1)
    assert calls["filter"] == "artist"
    assert results == [{"artist": "Some Artist", "browseId": "a1"}]


# --- get_library_playlists / get_playlist -------------------------------


def test_get_library_playlists_keeps_only_the_users_own():
    # Spotify returns followed playlists alongside owned ones, distinguishable
    # only by owner.id. Returning someone else's breaks two things: reading its
    # tracks 403s, and the exclusion set silently skips what it cannot read.
    yt = _client({
        "get_current_user": {"id": "me"},
        "get_playlists": [
            {"id": "p1", "name": "My Playlist", "owner": {"id": "me"}},
            {"id": "p2", "name": "Someone Else's", "owner": {"id": "stranger"}},
        ],
    })
    assert yt.get_library_playlists() == [{"playlistId": "p1", "title": "My Playlist"}]


def test_the_identity_lookup_happens_once_not_per_listing():
    calls = []
    yt = _client({
        "get_current_user": lambda **kw: calls.append(1) or {"id": "me"},
        "get_playlists": [{"id": "p1", "name": "Mine", "owner": {"id": "me"}}],
    })
    yt.get_library_playlists()
    yt.get_library_playlists()
    assert len(calls) == 1


def test_an_unavailable_identity_keeps_every_playlist():
    # Failing to learn who you are must not empty the library -- that would
    # void the exclusion set rather than merely narrowing it.
    yt = _client({
        "get_current_user": SpotifyMCPError("down"),
        "get_playlists": [{"id": "p1", "name": "Mine", "owner": {"id": "me"}}],
    })
    assert yt.get_library_playlists() == [{"playlistId": "p1", "title": "Mine"}]


def test_get_playlist_lm_routes_to_saved_tracks():
    yt = _client({"get_saved_tracks": [{"id": "t1", "name": "Liked Song", "artists": []}]})
    result = yt.get_playlist("LM", limit=None)
    assert result["tracks"] == [{"videoId": "t1", "title": "Liked Song", "artists": [], "album": None}]


def test_get_playlist_other_id_routes_to_playlist_tracks():
    calls = {}

    def fake_playlist_tracks(playlist_id, limit):
        calls["playlist_id"] = playlist_id
        return [{"id": "t2", "name": "Track", "artists": []}]

    yt = _client({"get_playlist_tracks": fake_playlist_tracks})
    result = yt.get_playlist("PL123", limit=50)
    assert calls["playlist_id"] == "PL123"
    assert result["tracks"][0]["videoId"] == "t2"


# --- get_watch_playlist --------------------------------------------------


def test_get_watch_playlist_includes_seed_and_recommendations():
    yt = _client({
        "get_track": {"id": "seed1", "name": "Seed Song", "artists": [{"name": "A", "id": "a1"}]},
        "get_recommendations": [{"id": "rec1", "name": "Rec Song", "artists": []}],
    })
    result = yt.get_watch_playlist(videoId="seed1", limit=25, radio=True)
    assert [t["videoId"] for t in result["tracks"]] == ["seed1", "rec1"]
    assert result["related"] == "seed1"


def test_get_watch_playlist_recommendations_unavailable_keeps_seed():
    yt = _client({
        "get_track": {"id": "seed1", "name": "Seed Song", "artists": []},
        "get_recommendations": SpotifyMCPError("recommendations endpoint restricted"),
    })
    result = yt.get_watch_playlist(videoId="seed1")
    assert [t["videoId"] for t in result["tracks"]] == ["seed1"]


def test_get_watch_playlist_seed_lookup_fails_recommendations_still_returned():
    yt = _client({
        "get_track": SpotifyMCPError("not found"),
        "get_recommendations": [{"id": "rec1", "name": "Rec Song", "artists": []}],
    })
    result = yt.get_watch_playlist(videoId="seed1")
    assert [t["videoId"] for t in result["tracks"]] == ["rec1"]


def test_get_watch_playlist_no_seed_id():
    yt = _client({"get_recommendations": []})
    result = yt.get_watch_playlist(videoId=None)
    assert result == {"tracks": [], "related": None}


# --- get_song_related ------------------------------------------------------


def test_get_song_related_full_chain():
    yt = _client({
        "get_track": {"id": "seed1", "name": "Seed", "artists": [{"name": "A", "id": "a1"}]},
        "get_related_artists": [{"id": "a2", "name": "Related Artist"}],
        "get_artist_top_tracks": [{"id": "rt1", "name": "Related Top Track", "artists": []}],
    })
    sections = yt.get_song_related("seed1")
    assert len(sections) == 1
    assert sections[0]["contents"][0]["videoId"] == "rt1"


def test_get_song_related_seed_lookup_fails():
    yt = _client({"get_track": SpotifyMCPError("not found")})
    assert yt.get_song_related("seed1") == []


def test_get_song_related_seed_has_no_artist():
    yt = _client({"get_track": {"id": "seed1", "name": "Seed", "artists": []}})
    assert yt.get_song_related("seed1") == []


def test_get_song_related_related_artists_call_fails():
    yt = _client({
        "get_track": {"id": "seed1", "name": "Seed", "artists": [{"name": "A", "id": "a1"}]},
        "get_related_artists": SpotifyMCPError("restricted"),
    })
    assert yt.get_song_related("seed1") == []


def test_get_song_related_one_artists_top_tracks_failure_skipped():
    def fake_top_tracks(artist_id):
        if artist_id == "a2":
            raise SpotifyMCPError("down")
        return [{"id": "rt2", "name": "OK Track", "artists": []}]

    yt = _client({
        "get_track": {"id": "seed1", "name": "Seed", "artists": [{"name": "A", "id": "a1"}]},
        "get_related_artists": [{"id": "a2", "name": "Bad"}, {"id": "a3", "name": "Good"}],
        "get_artist_top_tracks": fake_top_tracks,
    })
    sections = yt.get_song_related("seed1")
    assert [t["videoId"] for t in sections[0]["contents"]] == ["rt2"]


# --- get_artist ------------------------------------------------------------


def test_get_artist_shape():
    yt = _client({
        "get_artist_top_tracks": [{"id": "t1", "name": "Top Track", "artists": []}],
        "get_related_artists": [{"id": "a2", "name": "Related"}, {"id": None, "name": "No Id"}],
    })
    result = yt.get_artist("artist1")
    assert result["songs"]["browseId"] is None
    assert result["songs"]["results"][0]["videoId"] == "t1"
    assert result["related"]["results"] == [{"browseId": "a2"}]


def test_get_artist_degrades_gracefully_on_failures():
    yt = _client({
        "get_artist_top_tracks": SpotifyMCPError("down"),
        "get_related_artists": SpotifyMCPError("down"),
        "get_artist_albums": SpotifyMCPError("down"),
    })
    result = yt.get_artist("artist1")
    assert result == {"songs": {"browseId": None, "results": []}, "related": {"results": []}}


def test_the_album_walk_supplies_a_catalog_when_top_tracks_is_forbidden():
    # The measured case: on an app without Extended Quota Mode top tracks 403s,
    # and songs_by_artist returned 0 songs with no explanation at all.
    yt = _client({
        "get_artist_top_tracks": SpotifyMCPError("403 Forbidden"),
        "get_related_artists": SpotifyMCPError("403 Forbidden"),
        "get_artist_albums": [{"id": "al1", "name": "Discovery"}],
        "get_album_tracks": [
            {"id": "t1", "name": "One More Time", "artists": [{"name": "Daft Punk"}]},
            {"id": "t2", "name": "Aerodynamic", "artists": [{"name": "Daft Punk"}]},
        ],
    })
    songs = yt.get_artist("artist1")["songs"]["results"]
    assert [s["title"] for s in songs] == ["One More Time", "Aerodynamic"]
    # Album track objects carry no album of their own; without attaching it,
    # two different recordings of one title collapse into a single identity.
    assert songs[0]["album"] == {"name": "Discovery"}


def test_top_tracks_wins_when_it_is_allowed():
    # The album walk costs 1 + N calls, so it stays a fallback rather than
    # becoming the default on an app that still has the cheap endpoint.
    yt = _client({
        "get_artist_top_tracks": [{"id": "t9", "name": "Top Song", "artists": []}],
        "get_related_artists": [],
    })
    songs = yt.get_artist("artist1")["songs"]["results"]
    assert [s["title"] for s in songs] == ["Top Song"]


def test_one_unreadable_album_does_not_lose_the_rest_of_the_catalog():
    def album_tracks(album_id, limit=None):
        if album_id == "bad":
            raise SpotifyMCPError("gone")
        return [{"id": "t1", "name": "Good Song", "artists": []}]

    yt = _client({
        "get_artist_top_tracks": SpotifyMCPError("403"),
        "get_related_artists": [],
        "get_artist_albums": [{"id": "bad", "name": "Bad"}, {"id": "ok", "name": "OK"}],
        "get_album_tracks": album_tracks,
    })
    songs = yt.get_artist("artist1")["songs"]["results"]
    assert [s["title"] for s in songs] == ["Good Song"]


def test_a_track_on_two_releases_is_returned_once():
    yt = _client({
        "get_artist_top_tracks": SpotifyMCPError("403"),
        "get_related_artists": [],
        "get_artist_albums": [{"id": "single", "name": "Single"}, {"id": "lp", "name": "LP"}],
        "get_album_tracks": [{"id": "t1", "name": "Same Song", "artists": []}],
    })
    songs = yt.get_artist("artist1")["songs"]["results"]
    assert len(songs) == 1


# --- get_history -------------------------------------------------------


def test_get_history_unwraps_track_field():
    yt = _client({
        "get_recently_played": [
            {"track": {"id": "t1", "name": "Song", "artists": []}},
        ]
    })
    history = yt.get_history()
    assert history[0]["videoId"] == "t1"


def test_get_history_flat_item():
    yt = _client({"get_recently_played": [{"id": "t1", "name": "Song", "artists": []}]})
    assert yt.get_history()[0]["videoId"] == "t1"
