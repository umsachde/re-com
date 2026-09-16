"""The orchestrator's own judgement, checked without an agent or an account.

`scripts/orchestrate.py` needs credentials, a live backend, and tokens, so like
`smoke_all.py` it cannot run in CI -- which makes its verdict exactly the kind
of code that rots into always-passing. These tests cover the parts that decide
whether a run passed: what counts as satisfying each constraint, what the
context budget is allowed to throw away, and whether an agent can talk its way
to a pass by citing songs no tool returned.

The live half is verified by running it.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import orchestrate


def _song(vid, artist="A", energy=0.5, rated=True, title=None):
    song = {
        "videoId": vid,
        "title": title or f"Song {vid}",
        "artists": [artist],
        "rated": rated,
        "slot": 0,
    }
    if rated:
        song["mood"] = {"valence": 0.0, "energy": energy, "tension": 0.3, "depth": 0.4}
    return song


def _passing_set(n=20):
    # Distinct artists, energy rising across the set.
    return [_song(f"v{i}", artist=f"artist{i}", energy=0.2 + i * 0.03) for i in range(n)]


SPEC = {"count": 20, "max_per_artist": 2, "min_rated": 15}


# --- check ------------------------------------------------------------------


def test_a_clean_set_passes_every_constraint():
    result = orchestrate.check(_passing_set(), SPEC)
    assert result["passed"]
    assert all(c["passed"] for c in result["checks"].values())


def test_wrong_count_fails():
    result = orchestrate.check(_passing_set(19), SPEC)
    assert not result["passed"]
    assert result["checks"]["count"] == {"want": 20, "got": 19, "passed": False}


def test_a_third_song_by_one_artist_fails_the_cap():
    songs = _passing_set()
    for i in range(3):
        songs[i]["artists"] = ["Repeat Offender"]
    result = orchestrate.check(songs, SPEC)
    assert not result["passed"]
    assert result["checks"]["max_per_artist"]["got"] == 3
    assert result["checks"]["max_per_artist"]["worst_artist"] == "repeat offender"


def test_two_songs_by_one_artist_is_allowed():
    songs = _passing_set()
    songs[0]["artists"] = songs[1]["artists"] = ["Twice Is Fine"]
    assert orchestrate.check(songs, SPEC)["checks"]["max_per_artist"]["passed"]


def test_a_duplicated_song_fails_even_at_the_right_count():
    songs = _passing_set()
    songs[5] = dict(songs[4])
    result = orchestrate.check(songs, SPEC)
    assert not result["passed"]
    assert result["checks"]["distinct"]["got"] == 19


def test_too_much_filler_fails():
    songs = _passing_set()
    for song in songs[:6]:  # 14 rated, one short of the bar
        song["rated"] = False
        song.pop("mood")
    result = orchestrate.check(songs, SPEC)
    assert not result["passed"]
    assert result["checks"]["rated"] == {"want": 15, "got": 14, "passed": False}


def test_falling_energy_fails_the_arc():
    songs = [_song(f"v{i}", artist=f"artist{i}", energy=0.9 - i * 0.03) for i in range(20)]
    result = orchestrate.check(songs, SPEC)
    assert not result["passed"]
    assert not result["checks"]["energy_rises"]["passed"]
    assert result["checks"]["energy_rises"]["second_half"] < result["checks"]["energy_rises"]["first_half"]


def test_flat_energy_fails_the_arc():
    songs = [_song(f"v{i}", artist=f"artist{i}", energy=0.5) for i in range(20)]
    assert not orchestrate.check(songs, SPEC)["checks"]["energy_rises"]["passed"]


def test_too_few_rated_songs_to_judge_a_trend_is_a_failure_not_a_pass():
    """The dangerous shape: nothing to measure must never read as success."""
    songs = _passing_set()
    for song in songs[2:]:
        song["rated"] = False
        song.pop("mood")
    check = orchestrate.check(songs, SPEC)["checks"]["energy_rises"]
    assert not check["passed"]
    assert "too few" in check["reason"]


# --- the context budget -----------------------------------------------------


def _result_payload(n=20):
    return {
        "target": {"valence": 0.2, "energy": 0.8},
        "target_origin": "feeling",
        "notes": ["Only 12 songs genuinely matched this mood"],
        "match_quality": {"genuine": 12, "fluff_cap": 5},
        "seeds": [{"title": f"seed {i}", "artists": ["x"], "fit": 0.5} for i in range(6)],
        "filters": {"language": {"kept": 30, "dropped": 12}, "tempo": {}},
        "songs": [
            {**_song(f"v{i}", artist=f"artist{i}", energy=0.5),
             "slot_target": {"valence": 0.1, "energy": 0.6, "tension": 0.3, "depth": 0.4},
             "sources": ["radio", "graph_related"], "score": 3, "album": "An Album"}
            for i in range(n)
        ],
    }


def test_trim_keeps_the_signals_the_agent_replans_on():
    trimmed, _ = orchestrate.trim(_result_payload(), budget=2000)
    assert trimmed["notes"] == ["Only 12 songs genuinely matched this mood"]
    assert trimmed["match_quality"] == {"genuine": 12, "fluff_cap": 5}


def test_trim_drops_the_metadata_dumps():
    trimmed, report = orchestrate.trim(_result_payload(), budget=2000)
    assert "seeds" not in trimmed
    assert "filters" not in trimmed
    assert report["bytes_after"] < report["bytes_before"]


def test_trim_keeps_every_song_when_compaction_is_enough():
    trimmed, report = orchestrate.trim(_result_payload(20), budget=2000)
    assert report["songs_dropped"] == 0
    assert len(trimmed["songs"]) == 20
    assert all(isinstance(line, str) for line in trimmed["songs"])


def test_a_compacted_song_still_says_what_it_is():
    trimmed, _ = orchestrate.trim(_result_payload(1), budget=2000)
    line = trimmed["songs"][0]
    assert "v0" in line and "rated" in line and "energy" in line


def test_trim_truncates_only_when_compaction_was_not_enough_and_says_so():
    trimmed, report = orchestrate.trim(_result_payload(40), budget=500)
    assert report["songs_dropped"] > 0
    assert "dropped to fit the context budget" in trimmed["truncated"]


def test_a_trimmed_result_actually_fits_the_budget_including_its_own_notice():
    """The notice counts. Appending it after the fit loop put every truncated
    result back over the cap it had just been trimmed to meet."""
    for budget in (2000, 1000, 500):
        trimmed, report = orchestrate.trim(_result_payload(40), budget=budget)
        assert len(json.dumps(trimmed)) <= budget, f"budget {budget} overshot"
        assert not report["over_budget"]


def test_a_budget_below_the_untrimmable_floor_is_reported_not_faked():
    """`notes` and `match_quality` are never trimmed, so a tiny budget cannot
    be met -- the report must say so rather than claim a fit."""
    trimmed, report = orchestrate.trim(_result_payload(40), budget=50)
    assert report["over_budget"]
    assert trimmed["notes"] == ["Only 12 songs genuinely matched this mood"]


def test_trim_leaves_a_payload_with_no_songs_alone():
    payload = {"provider": "youtube", "mood_supported": True}
    trimmed, report = orchestrate.trim(payload, budget=10)
    assert trimmed == payload
    assert not report["compacted"]


# --- the registry: the verdict reads tool output, not the agent's claims -----


def test_registry_resolves_ids_to_what_the_tool_actually_reported():
    registry = orchestrate.Registry()
    registry.capture(_result_payload(3))
    # The agent claims its pick was rated; the tool said what it said.
    songs, unknown = registry.resolve([{"videoId": "v1", "title": "whatever", "rated": False}])
    assert unknown == []
    assert songs[0]["rated"] is True
    assert songs[0]["title"] == "Song v1"


def test_an_invented_song_is_reported_not_silently_dropped():
    registry = orchestrate.Registry()
    registry.capture(_result_payload(2))
    songs, unknown = registry.resolve([{"videoId": "v0"}, {"videoId": "hallucinated"}])
    assert len(songs) == 1
    assert unknown == ["hallucinated"]


def test_registry_keeps_the_first_sighting_of_a_song():
    registry = orchestrate.Registry()
    assert registry.capture(_result_payload(3)) == 3
    assert registry.capture(_result_payload(3)) == 0


# --- parsing the agent's answer ---------------------------------------------


def test_parse_answer_reads_the_fenced_block():
    text = 'Here is the set.\n\n```json\n{"songs": [{"videoId": "a"}, {"videoId": "b"}]}\n```'
    assert orchestrate.parse_answer(text) == [{"videoId": "a"}, {"videoId": "b"}]


def test_parse_answer_prefers_the_last_block_over_working_shown_earlier():
    text = (
        '```json\n{"songs": [{"videoId": "draft"}]}\n```\n'
        'On reflection:\n```json\n{"songs": [{"videoId": "final"}]}\n```'
    )
    assert orchestrate.parse_answer(text) == [{"videoId": "final"}]


def test_parse_answer_survives_a_malformed_block():
    assert orchestrate.parse_answer("```json\n{not json}\n```") == []
    assert orchestrate.parse_answer("no block at all") == []


def test_mcp_payload_unwraps_the_envelope():
    envelope = {"content": [{"type": "text", "text": json.dumps({"songs": [], "notes": ["hi"]})}]}
    assert orchestrate._mcp_payload(envelope) == {"songs": [], "notes": ["hi"]}


def test_mcp_payload_passes_through_a_bare_dict():
    assert orchestrate._mcp_payload({"songs": []}) == {"songs": []}
