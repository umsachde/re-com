"""Unit tests for match.py -- the shared song-identity rules.

Pure functions, no network, no store. signals.py and tempo.py delegate here,
and their own existing tests (test_v2.py's same_song/collapse cases,
test_filters.py's _same_title case) are what prove the delegation kept their
behaviour; these cover match.py's own surface, including the cases only v6's
graph bridge exercises.
"""

import match


# --- song_key ---------------------------------------------------------------


def test_song_key_collapses_upload_variants():
    # The case that forced this to exist: YouTube carries one track many times.
    base = match.song_key("Dead and Gone")
    assert match.song_key("Dead and Gone (feat. Justin Timberlake)") == base
    assert match.song_key("Dead and Gone [Remastered]") == base
    assert match.song_key("Dead and Gone - Official Video") == base


def test_song_key_keeps_genuinely_different_songs_apart():
    assert match.song_key("Kryptonite") != match.song_key("Here Without You")


def test_song_key_strips_punctuation_and_case():
    assert match.song_key("Don't Stop!") == match.song_key("dont stop")


def test_song_key_of_only_punctuation_is_empty():
    # Falls through to the callers' "no usable key" branches rather than
    # producing a key that matches every other punctuation-only title.
    assert match.song_key("!!!") == ""


# --- artist_matches ---------------------------------------------------------


def test_artist_matches_is_loose_in_both_directions():
    assert match.artist_matches("3 Doors Down Topic", "3 Doors Down")
    assert match.artist_matches("3 Doors Down", "3 Doors Down Topic")


def test_unknown_wanted_artist_matches_anything():
    # A caller that doesn't know who it's looking for must not get zero results.
    assert match.artist_matches("Anyone At All", None)
    assert match.artist_matches("Anyone At All", "")


def test_missing_candidate_credit_matches_nothing():
    # Deliberately asymmetric with the case above, and a fix rather than a
    # faithful port: the old bare-substring test made "" match everything
    # (`"" in b` is always True), so a Deezer hit with no artist name passed
    # the artist gate and could attach another song's BPM.
    assert not match.artist_matches("", "AP Dhillon")
    assert not match.artist_matches(None, "AP Dhillon")


def test_artist_matches_rejects_unrelated_credits():
    assert not match.artist_matches("Big Boi", "3 Doors Down")


# --- artist_names_match -----------------------------------------------------


def test_artist_names_match_finds_any_credit():
    assert match.artist_names_match(["Karan Aujla", "Divine"], ["divine"])


def test_artist_names_match_skips_empty_credits():
    assert not match.artist_names_match([None, ""], ["divine"])


# --- same_song --------------------------------------------------------------


def test_same_song_matches_re_uploads():
    assert match.same_song("Kryptonite", "3 Doors Down", "Kryptonite (Official Video)", "3 Doors Down")


def test_same_song_accepts_a_missing_artist_on_either_side():
    # Catalogues routinely omit the credit; refusing on that basis would let
    # known duplicates through.
    assert match.same_song("Bulleya", None, "Bulleya", "Amit Mishra")
    assert match.same_song("Bulleya", "Amit Mishra", "Bulleya", None)


def test_same_song_does_not_over_match():
    assert not match.same_song("Kryptonite", "3 Doors Down", "Kryptonite", "Big Boi")
    assert not match.same_song("Kryptonite", "3 Doors Down", "Here Without You", "3 Doors Down")
    assert not match.same_song(None, "x", "Kryptonite", "x")
    assert not match.same_song("Kryptonite", "x", None, "x")


# --- same_title -------------------------------------------------------------


def test_same_title_requires_both_sides():
    assert match.same_title("Kamariya (From \"Stree\")", "Kamariya")
    assert not match.same_title("Kamariya", None)
    assert not match.same_title(None, "Kamariya")


# --- build_index / matches_any ----------------------------------------------
#
# The v6 shadow exclusion set: graph candidates arrive as title+artist text
# with no provider id, so the library check has to happen on text first.


def test_build_index_accepts_joined_and_split_artist_credits():
    # The store keeps artists joined; provider payloads keep them split.
    index = match.build_index([("Excuses", "AP Dhillon"), ("Brown Munde", ["AP Dhillon", "Gurinder Gill"])])
    assert match.matches_any("Excuses", "AP Dhillon", index)
    assert match.matches_any("Brown Munde", "Gurinder Gill", index)


def test_matches_any_ignores_upload_variants():
    index = match.build_index([("Excuses", "AP Dhillon")])
    assert match.matches_any("Excuses (Official Video)", "AP Dhillon", index)


def test_matches_any_rejects_a_different_artists_song_of_the_same_name():
    index = match.build_index([("Kryptonite", "3 Doors Down")])
    assert not match.matches_any("Kryptonite", "Big Boi", index)


def test_matches_any_is_permissive_when_an_artist_is_unknown():
    # Erring toward True: a false positive drops one candidate from a pool of
    # hundreds, a false negative breaks the never-recommend-a-library-song
    # guarantee this project exists for.
    index = match.build_index([("Excuses", "AP Dhillon")])
    assert match.matches_any("Excuses", None, index)
    assert match.matches_any("Excuses", "AP Dhillon", match.build_index([("Excuses", None)]))


def test_matches_any_misses_are_real_misses():
    index = match.build_index([("Excuses", "AP Dhillon")])
    assert not match.matches_any("Brown Munde", "AP Dhillon", index)
    assert not match.matches_any(None, "AP Dhillon", index)
    assert not match.matches_any("!!!", "AP Dhillon", index)


def test_build_index_skips_unusable_rows():
    index = match.build_index([(None, "x"), ("", "x"), ("!!!", "x")])
    assert index == {}
