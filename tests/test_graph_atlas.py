"""Unit tests for the provider-neutral mood atlas.

No network -- `graph._get` is faked. The graph atlas is what makes mood work on
a backend that has no editorial mood taxonomy of its own, which before v6 meant
mood was YouTube-only.
"""

import graph
import graph_atlas
import label
import moodspace
import store


class _FakeDeezer:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                return payload
        return {"data": []}


def _dz_track(track_id, title, artist_name, artist_id=1):
    return {"id": track_id, "title": title, "artist": {"name": artist_name, "id": artist_id}}


def _wire(monkeypatch, routes):
    fake = _FakeDeezer(routes)
    monkeypatch.setattr(graph, "_get", fake)
    return fake


def _sad_playlist_world():
    return {
        "/search/playlist": {"data": [{"id": 42, "title": "Bollywood Sad", "nb_tracks": 2}]},
        "/playlist/42/tracks": {"data": [
            _dz_track(1, "Channa Mereya", "Pritam"),
            _dz_track(2, "Bulleya", "Amit Mishra", artist_id=2),
        ]},
    }


# --- query generation -------------------------------------------------------


def test_queries_cover_every_placeable_mood():
    moods = {mood for mood, _ in graph_atlas.queries()}
    assert moods == set(graph_atlas.MOOD_QUERIES) <= set(moodspace.ANCHORS)


def test_queries_are_not_english_only():
    """The 4.1% coverage problem, addressed directly.

    YouTube's mood playlists barely touch this library's Punjabi and Bollywood
    catalogue, so the neutral atlas searches for it by name.
    """
    all_queries = {query for _, query in graph_atlas.queries()}
    assert any("punjabi" in q for q in all_queries)
    assert any("bollywood" in q for q in all_queries)
    assert any("hindi" in q for q in all_queries)


def test_every_mood_keeps_a_bare_english_query():
    sad = {q for mood, q in graph_atlas.queries() if mood == "Sad"}
    assert "sad songs" in sad


# --- crawl ------------------------------------------------------------------


def test_crawl_records_playlists_and_memberships(graph_db, monkeypatch):
    _wire(monkeypatch, _sad_playlist_world())
    stats = graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    assert stats["playlists"] == 1
    assert stats["tracks"] == 2
    assert graph_atlas.coverage(graph_db)["tracks"] == 2


def test_crawl_resumes_rather_than_repeating_work(graph_db, monkeypatch):
    # A crawl this size will be interrupted; starting over would be
    # unacceptable. Same lesson already baked into store.atlas_crawl.
    fake = _wire(monkeypatch, _sad_playlist_world())
    graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    before = len(fake.calls)
    stats = graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    assert stats["skipped"] >= 1
    assert len(fake.calls) > before or stats["queries"] <= 1


def test_a_query_that_found_nothing_is_still_checked_off(graph_db, monkeypatch):
    """"Asked, nothing there" must not look like "never asked".

    Measured on the first full crawl: 231 queries ran but only 167 were
    recorded, because the resume point was inferred from the playlists a query
    produced. A re-run would have paid for 64 known-empty searches again.
    """
    fake = _wire(monkeypatch, {"/search/playlist": {"data": []}})
    graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    assert len(graph_atlas.crawled_queries(graph_db)) == 1

    before = len(fake.calls)
    stats = graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    assert stats["queries"] == 1, "should move on to the next query, not redo the empty one"
    assert len(fake.calls) > before


def test_coverage_counts_attempted_queries_not_productive_ones(graph_db, monkeypatch):
    _wire(monkeypatch, {"/search/playlist": {"data": []}})
    graph_atlas.crawl(graph_db, limit=3, sleep=lambda _s: None)
    assert graph_atlas.coverage(graph_db)["queries_crawled"] == 3


def test_fresh_crawl_ignores_the_resume_point(graph_db, monkeypatch):
    _wire(monkeypatch, _sad_playlist_world())
    graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    stats = graph_atlas.crawl(graph_db, limit=1, resume=False, sleep=lambda _s: None)
    assert stats["skipped"] == 0


# --- materialize ------------------------------------------------------------


def test_materialize_places_tracks_in_the_mood_space(graph_db, monkeypatch):
    _wire(monkeypatch, _sad_playlist_world())
    graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    written = graph_atlas.materialize_moods(graph_db)
    assert written == 2

    mood = graph_atlas.get_mood(graph_db, 1)
    assert mood["valence"] == moodspace.ANCHORS["Sad"]["valence"]


def test_materialized_confidence_is_discounted_below_the_native_atlas(graph_db, monkeypatch):
    # A playlist merely titled "sad songs" was named by a stranger; a YouTube
    # mood playlist was filed by the service under a taxonomy.
    _wire(monkeypatch, _sad_playlist_world())
    graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    graph_atlas.materialize_moods(graph_db)
    assert graph_atlas.get_mood(graph_db, 1)["confidence"] <= graph_atlas.GRAPH_ATLAS_CONFIDENCE


def test_materialize_is_rerunnable_after_retuning_anchors(graph_db, monkeypatch):
    _wire(monkeypatch, _sad_playlist_world())
    graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    assert graph_atlas.materialize_moods(graph_db) == 2
    assert graph_atlas.materialize_moods(graph_db) == 2


# --- propagation to a provider ----------------------------------------------


def test_moods_reach_provider_tracks_through_the_id_bridge(db, graph_db, monkeypatch):
    """The whole point: a Deezer-keyed mood becomes a provider-keyed one.

    Labelling work is done once on the neutral graph no matter how many
    backends are connected.
    """
    _wire(monkeypatch, {
        **_sad_playlist_world(),
        "/search?": {"data": [_dz_track(1, "Channa Mereya", "Pritam")]},
    })
    graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    graph_atlas.materialize_moods(graph_db)

    stats = graph_atlas.propagate_to_provider(
        db, graph_db,
        [{"video_id": "yt1", "title": "Channa Mereya", "artists": "Pritam"}],
        sleep=lambda _s: None,
    )
    assert stats["labeled"] == 1

    resolved = label.resolve(db, "yt1")
    assert resolved["source"] == graph_atlas.SOURCE
    assert resolved["vector"]["valence"] == moodspace.ANCHORS["Sad"]["valence"]


def test_propagation_counts_tracks_deezer_does_not_have(db, graph_db, monkeypatch):
    _wire(monkeypatch, {"/search": {"data": []}})
    stats = graph_atlas.propagate_to_provider(
        db, graph_db,
        [{"video_id": "yt1", "title": "Unknown Song", "artists": "Nobody"}],
        sleep=lambda _s: None,
    )
    assert stats == {"labeled": 0, "unresolved": 1, "no_mood": 0}


def test_propagation_separates_resolved_but_unlabelled_from_unresolved(db, graph_db, monkeypatch):
    # Two different gaps that must not be conflated: Deezer has the song but no
    # mood evidence for it, versus Deezer not having the song at all.
    _wire(monkeypatch, {"/search?": {"data": [_dz_track(99, "Some Song", "X")]}})
    stats = graph_atlas.propagate_to_provider(
        db, graph_db,
        [{"video_id": "yt1", "title": "Some Song", "artists": "X"}],
        sleep=lambda _s: None,
    )
    assert stats == {"labeled": 0, "unresolved": 0, "no_mood": 1}


def test_propagation_takes_the_primary_artist_from_a_joined_credit(db, graph_db, monkeypatch):
    # The store keeps artists joined with " & "; Deezer search wants one name.
    fake = _wire(monkeypatch, {
        **_sad_playlist_world(),
        "/search?": {"data": [_dz_track(1, "Channa Mereya", "Pritam")]},
    })
    graph_atlas.crawl(graph_db, limit=1, sleep=lambda _s: None)
    graph_atlas.materialize_moods(graph_db)
    graph_atlas.propagate_to_provider(
        db, graph_db,
        [{"video_id": "yt1", "title": "Channa Mereya", "artists": "Pritam & Arijit Singh"}],
        sleep=lambda _s: None,
    )
    assert any("Pritam" in url and "Arijit" not in url for url in fake.calls)


def test_propagation_skips_a_track_with_no_title(db, graph_db, monkeypatch):
    fake = _wire(monkeypatch, {})
    stats = graph_atlas.propagate_to_provider(
        db, graph_db, [{"video_id": "yt1", "title": None, "artists": "X"}],
        sleep=lambda _s: None,
    )
    assert stats["unresolved"] == 1
    assert fake.calls == []


# --- priority ---------------------------------------------------------------


def test_the_native_atlas_still_outranks_the_neutral_one(db):
    """Best available source wins outright -- the rule PLAN_V2 already used.

    YouTube's own taxonomy is stronger evidence than a playlist title, so where
    both exist the native reading must win.
    """
    store.put_track_mood(db, "yt1", "atlas", moodspace.vector(valence=0.8), 0.9)
    store.put_track_mood(db, "yt1", graph_atlas.SOURCE, moodspace.vector(valence=-0.8), 0.9)
    assert label.resolve(db, "yt1")["source"] == "atlas"


def test_the_neutral_atlas_outranks_artist_propagation(db):
    # Direct evidence about this song beats an inference from its artist.
    store.put_track_mood(db, "yt1", "artist", moodspace.vector(valence=0.8), 0.9)
    store.put_track_mood(db, "yt1", graph_atlas.SOURCE, moodspace.vector(valence=-0.8), 0.9)
    assert label.resolve(db, "yt1")["source"] == graph_atlas.SOURCE


def test_lyrics_still_outrank_both_atlases(db):
    store.put_track_mood(db, "yt1", graph_atlas.SOURCE, moodspace.vector(valence=0.8), 0.9)
    store.put_track_mood(db, "yt1", "lyrics", moodspace.vector(valence=-0.8), 0.9)
    assert label.resolve(db, "yt1")["source"] == "lyrics"
