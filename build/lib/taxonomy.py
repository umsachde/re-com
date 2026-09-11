"""Genre and language, so results can be filtered to what you want to hear.

"Find songs like this Punjabi track, but only English ones" needs a language
label on every candidate. Nothing in the YouTube Music API returns one, so it
is assembled from four layers, strongest first:

  script    The title is written in Devanagari, Gurmukhi, Arabic, Hangul,
            Kana or Han. Direct evidence, needs no crawl and no network.
  library   The user's own "C - <genre>" playlists. A human already filed
            these, and their filing beats any inference -- it is also the only
            source that separates Punjabi from Hindi, which YouTube's own
            taxonomy lumps together as "Bollywood & Indian".
  genre     Membership in YouTube's genre-category pages.
  artist    The dominant genre of everything else by that artist. Unlike
            tempo, this propagates well: artists rarely switch language.

ytmusicapi's get_mood_playlists() raises KeyError on 25 of the 27 genre
categories -- those pages lead with a "Songs" carousel of track items and its
parser assumes every item is a playlist. Rather than lose the genre data, the
sections are parsed here directly, which also harvests those songs as
genre-labelled tracks for free.

A caveat worth keeping in view: this infers *language* from *genre*, which is
approximate. "Dance & electronic" is frequently instrumental, and
"Reggae & caribbean" is usually English. Treat the label as a strong hint,
not a certainty.
"""

import unicodedata
from typing import Any, Iterable

import store

CATEGORY_BROWSE_ID = "FEmusic_moods_and_genres_category"

# Genre -> language group. English is the default for genres that are
# overwhelmingly anglophone; only genres that clearly signal another language
# are mapped away from it.
GENRE_LANGUAGE = {
    "Bollywood & Indian": "indian",
    "Arabic": "arabic",
    "K-Pop": "korean",
    "J-Pop": "japanese",
    "Mandopop & cantopop": "chinese",
    "Francophone": "french",
    "Latin": "latin",
    "Brazilian": "portuguese",
    "African": "african",
    "Blues": "english", "Christian & gospel": "english", "Country & Americana": "english",
    "Folk & acoustic": "english", "Hip-hop": "english", "Indie & alternative": "english",
    "Jazz": "english", "Metal": "english", "Pop": "english", "R&B & soul": "english",
    "Rock": "english", "Songwriters & producers": "english", "Decades": "english",
    "Reggae & caribbean": "english", "Soundtracks & musicals": "english",
    # Deliberately unmapped: Classical, Family, Dance & electronic -- frequently
    # instrumental, so claiming a language for them would be inventing one.
}

# Keywords in the user's own playlist names, which are finer-grained than
# YouTube's taxonomy -- and matched loosely on purpose, because real playlist
# names are typed by hand ("Punjabu" is a real playlist in this library).
LIBRARY_NAME_LANGUAGE = [
    ("punjab", "punjabi"),
    ("bollywood", "hindi"),
    ("hindi", "hindi"),
    ("desi", "hindi"),
    ("arabic", "arabic"),
    ("k-pop", "korean"),
    ("latin", "latin"),
]

# How much each layer's evidence counts when voting on a language.
#
# The ordering matters more than the numbers. English is deliberately worth
# almost nothing: YouTube files Punjabi and Hindi rap under "Hip-hop", so
# treating an English-genre hit as a real vote labelled Sidhu Moose Wala,
# DIVINE and Karan Aujla as English. English is now what you get when no
# language-bearing evidence exists at all, rather than something that can
# outvote it.
SOURCE_WEIGHT = {"script": 100.0, "library": 50.0, "genre": 10.0, "genre_english": 1.0}

# Script ranges that identify a language outright.
_SCRIPT_LANGUAGE = [
    ("GURMUKHI", "punjabi"),
    ("DEVANAGARI", "hindi"),
    ("ARABIC", "arabic"),
    ("HANGUL", "korean"),
    ("HIRAGANA", "japanese"), ("KATAKANA", "japanese"),
    ("CJK", "chinese"),
]

# Languages that count as "not English" when filtering for English only.
NON_ENGLISH = {"indian", "punjabi", "hindi", "arabic", "korean", "japanese",
               "chinese", "french", "latin", "portuguese", "african"}

SOURCE_SCRIPT, SOURCE_LIBRARY, SOURCE_GENRE, SOURCE_ARTIST = "script", "library", "genre", "artist"
_PRIORITY = {SOURCE_SCRIPT: 0, SOURCE_LIBRARY: 1, SOURCE_GENRE: 2, SOURCE_ARTIST: 3}

ARTIST_MIN_LABELLED = 2


def script_language(text: str | None) -> str | None:
    """Identify a language from the writing system of a title, if it says so."""
    if not text:
        return None
    counts: dict[str, int] = {}
    for char in text:
        if not char.isalpha():
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            continue
        for prefix, language in _SCRIPT_LANGUAGE:
            if name.startswith(prefix):
                counts[language] = counts.get(language, 0) + 1
                break
    return max(counts, key=counts.get) if counts else None


# --- parsing YouTube's genre pages -----------------------------------------


def _runs_text(node: dict[str, Any]) -> str:
    return "".join(run.get("text", "") for run in (node or {}).get("runs", []))


def _parse_song_item(item: dict[str, Any]) -> dict[str, Any] | None:
    video_id = (item.get("playlistItemData") or {}).get("videoId")
    if not video_id:
        return None
    columns = item.get("flexColumns") or []
    texts = [
        _runs_text((c.get("musicResponsiveListItemFlexColumnRenderer") or {}).get("text", {}))
        for c in columns
    ]
    # The artist column carries a trailing "• 13M views"; keep only the credit.
    credit = texts[1].split(" • ")[0].strip() if len(texts) > 1 and texts[1] else None
    return {
        "videoId": video_id,
        "title": texts[0] if texts else None,
        "artists": [credit] if credit else [],
    }


def _parse_playlist_item(item: dict[str, Any]) -> str | None:
    endpoint = ((item.get("title") or {}).get("runs") or [{}])[0].get("navigationEndpoint") or {}
    browse_id = (endpoint.get("browseEndpoint") or {}).get("browseId")
    return browse_id[2:] if browse_id and browse_id.startswith("VL") else browse_id


def genre_page(yt: Any, params: str) -> dict[str, list]:
    """Songs and playlists on one genre category page.

    Written against the raw browse response because ytmusicapi's parser raises
    KeyError on 25 of 27 genre categories -- their leading "Songs" carousel
    holds track items where it expects playlists.
    """
    response = yt._send_request("browse", {"browseId": CATEGORY_BROWSE_ID, "params": params})
    try:
        tabs = response["contents"]["singleColumnBrowseResultsRenderer"]["tabs"]
        sections = tabs[0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"]
    except (KeyError, IndexError, TypeError):
        return {"songs": [], "playlists": []}

    songs, playlists = [], []
    for section in sections:
        shelf = section.get("musicCarouselShelfRenderer") or section.get("gridRenderer") or {}
        for item in shelf.get("contents") or shelf.get("items") or []:
            if "musicResponsiveListItemRenderer" in item:
                song = _parse_song_item(item["musicResponsiveListItemRenderer"])
                if song:
                    songs.append(song)
            elif "musicTwoRowItemRenderer" in item:
                playlist_id = _parse_playlist_item(item["musicTwoRowItemRenderer"])
                # Albums and videos also render this way; only playlist ids are
                # fetchable as playlists.
                if playlist_id and playlist_id.startswith(("PL", "RD", "OLAK", "VLPL")):
                    playlists.append(playlist_id)
    return {"songs": songs, "playlists": playlists}


def genre_params(yt: Any) -> dict[str, str]:
    categories = yt.get_mood_categories()
    return {c["title"]: c["params"] for c in categories.get("Genres", []) if c.get("params")}


def crawl_genres(
    yt: Any,
    conn: Any,
    playlists_per_genre: int = 12,
    sleep=None,
    on_progress=None,
) -> dict[str, int]:
    """Harvest genre-labelled tracks: the songs listed on each genre page, plus
    a sample of that genre's playlists."""
    import time as _time

    sleep = sleep or _time.sleep
    stats = {"genres": 0, "songs": 0, "playlists": 0}

    for genre, params in genre_params(yt).items():
        try:
            page = genre_page(yt, params)
        except Exception:  # noqa: BLE001 - one bad genre must not stop the crawl
            continue
        stats["genres"] += 1

        if page["songs"]:
            store.upsert_tracks(conn, page["songs"])
            stats["songs"] += store.record_genre(conn, genre, [s["videoId"] for s in page["songs"]])
        sleep(0.25)

        for playlist_id in page["playlists"][:playlists_per_genre]:
            try:
                full = yt.get_playlist(playlist_id, limit=100)
            except Exception:  # noqa: BLE001
                continue
            tracks = [t for t in full.get("tracks", []) if t.get("videoId")]
            store.upsert_tracks(conn, tracks)
            stats["songs"] += store.record_genre(conn, genre, [t["videoId"] for t in tracks])
            stats["playlists"] += 1
            sleep(0.25)

        if on_progress:
            on_progress({**stats, "genre": genre})
    return stats


# --- resolving a language --------------------------------------------------


def _library_language(conn: Any, video_id: str) -> str | None:
    """The language implied by the playlists the user filed this song under.

    Matches any playlist whose name contains a language keyword, not just the
    tidy "C - <genre>" ones -- people name playlists by hand.
    """
    for title in store.library_playlists_for(conn, video_id):
        lowered = title.lower()
        for keyword, language in LIBRARY_NAME_LANGUAGE:
            if keyword in lowered:
                return language
    return None


def _genre_votes(conn: Any, video_id: str) -> dict[str, float]:
    votes: dict[str, float] = {}
    for genre in store.genres_for(conn, video_id):
        language = GENRE_LANGUAGE.get(genre)
        if not language:
            continue
        weight = SOURCE_WEIGHT["genre_english"] if language == "english" else SOURCE_WEIGHT["genre"]
        votes[language] = votes.get(language, 0.0) + weight
    return votes


def language_votes(conn: Any, video_id: str, title: str | None = None, artists: str | None = None) -> dict[str, float]:
    """Weighted language evidence for one song, across every layer."""
    if title is None or artists is None:
        track = store.get_track(conn, video_id) or {}
        title = track.get("title") if title is None else title
        artists = track.get("artists") if artists is None else artists

    votes = _genre_votes(conn, video_id)

    from_library = _library_language(conn, video_id)
    if from_library:
        votes[from_library] = votes.get(from_library, 0.0) + SOURCE_WEIGHT["library"]

    from_script = script_language(title) or script_language(artists)
    if from_script:
        votes[from_script] = votes.get(from_script, 0.0) + SOURCE_WEIGHT["script"]

    return votes


def _source_for(weight: float) -> str:
    if weight >= SOURCE_WEIGHT["script"]:
        return SOURCE_SCRIPT
    if weight >= SOURCE_WEIGHT["library"]:
        return SOURCE_LIBRARY
    return SOURCE_GENRE


def resolve_language(conn: Any, video_id: str, title: str | None = None, artists: str | None = None) -> dict[str, Any] | None:
    """Best available language for one song, with the layer that produced it."""
    votes = language_votes(conn, video_id, title, artists)
    if not votes:
        return None
    best = max(votes, key=votes.get)
    return {"language": best, "source": _source_for(votes[best])}


def artist_languages(conn: Any) -> dict[str, str]:
    """Lead artist -> dominant language, from every directly-evidenced song.

    Artists rarely switch language, so this generalises well -- and it is what
    makes the filter work on songs nobody has ever labelled.
    """
    import label

    votes: dict[str, dict[str, float]] = {}
    for row in conn.execute("SELECT video_id, title, artists FROM track WHERE artists IS NOT NULL"):
        artist = label.primary_artist(row["artists"])
        if not artist:
            continue
        for language, weight in language_votes(conn, row["video_id"], row["title"], row["artists"]).items():
            votes.setdefault(artist, {})
            votes[artist][language] = votes[artist].get(language, 0.0) + weight

    # Summing weights rather than counting tracks is what stops six "Hip-hop"
    # hits from overruling one playlist the user themselves filed as Punjabi.
    return {artist: max(tally, key=tally.get) for artist, tally in votes.items() if tally}


def as_languages(value: Any) -> list[str] | None:
    """Normalise a language argument to a list.

    The tools take `list[str]`, but they're called by an LLM, and a bare
    `language="english"` is an easy mistake to make. Strings are iterable, so
    without this it silently becomes a filter for the languages "e", "n", "g",
    "l", "i", "s", "h" -- which matches nothing and reports itself as
    `Language filter (e, n, g, l, i, s, h): kept 0`.

    Producing a nonsense filter that looks like a working one is precisely the
    failure mode this project keeps guarding against, so a single string is
    read as what it obviously means.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value] if value.strip() else None
    out = [str(v) for v in value if str(v).strip()]
    return out or None


def matches(language: str | None, wanted: Iterable[str] | None, exclude: Iterable[str] | None) -> bool | None:
    """Whether a song passes a language filter. None means unknown.

    Unknown is never silently treated as a failure -- callers decide, and the
    honest default is to keep it and say so, because Deezer-style data gaps
    fall hardest on exactly the catalogue a language filter is about.
    """
    if language is None:
        return None
    if wanted:
        wanted = {w.lower() for w in wanted}
        if "english" in wanted and language in NON_ENGLISH:
            return False
        return language.lower() in wanted
    if exclude:
        return language.lower() not in {e.lower() for e in exclude}
    return True
