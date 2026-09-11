"""The similarity metric's own arithmetic, checked without a live account.

`scripts/quality_check.py --similarity` needs credentials and a real library,
so like the smoke harness it cannot run in CI -- the same rot risk applies, and
for a measurement the failure is worse than silence: a number that is quietly
wrong still gets written into PLAN.md as a baseline and argued from later.

These cover only the pure judgement -- what counts as corroborated, what counts
as concentrated, what the graph is credited with -- not the live pipeline.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import quality_check


def _song(title, artist, score, sources):
    return {"title": title, "artists": [artist], "score": score, "sources": sources}


MIXED = [
    _song("A", "X", 3, ["radio", "related", "artist"]),
    _song("B", "X", 2, ["radio", "graph_artist"]),
    _song("C", "Y", 1, ["graph_radio"]),
    _song("D", "Z", 1, ["graph_artist", "graph_related"]),
]


def test_corroborated_counts_picks_more_than_one_pair_agreed_on():
    agree = quality_check._agreement(MIXED, ceiling=6)
    assert agree["corroborated"] == 0.5
    assert agree["histogram"] == {1: 2, 2: 1, 3: 1}
    assert agree["mean"] == 1.75


def test_agreement_is_reported_against_its_ceiling():
    """The ceiling is the whole reason this is readable across backends: the
    same pool scores identically but normalizes differently on a backend with
    fewer signals, and without the ceiling that reads as a regression."""
    youtube = quality_check._agreement(MIXED, ceiling=6)
    spotify = quality_check._agreement(MIXED, ceiling=3)
    assert youtube["mean"] == spotify["mean"]
    assert youtube["normalized"] < spotify["normalized"]
    assert (youtube["ceiling"], spotify["ceiling"]) == (6, 3)


def test_concentration_sees_one_artist_owning_the_result():
    """HHI reads the whole distribution; max_artist_share catches the hog. A
    result split evenly must score lower than one artist taking half."""
    even = [_song(t, a, 1, []) for t, a in zip("ABCD", "WXYZ")]
    assert quality_check._concentration(even)["hhi"] < quality_check._concentration(MIXED)["hhi"]
    assert quality_check._concentration(MIXED)["max_artist_share"] == 0.5
    assert quality_check._concentration(even)["max_artist_share"] == 0.25


def test_graph_is_credited_only_for_picks_no_native_signal_had():
    """B had a native signal AND a graph one, so the graph did not add it --
    crediting the graph for anything it merely co-signed would overstate what
    turning it off would cost."""
    assert quality_check._graph_only_share(MIXED) == 0.5


def test_empty_results_report_none_rather_than_zero():
    """Nothing returned is not the same as nothing agreeing. A 0.0 here would
    average into a headline number as though it were a measurement."""
    assert quality_check._agreement([], 6)["corroborated"] is None
    assert quality_check._agreement([], 6)["n"] == 0
    assert quality_check._concentration([])["hhi"] is None
    assert quality_check._graph_only_share([]) is None


def test_ab_reports_churn_once_because_displacement_was_degenerate():
    """The first live run killed the metric this replaced. Both arms truncate
    to `limit`, so when both fill up "added" and "displaced" are the same
    number by construction -- 37 == 37 across ten YouTube cases, arithmetic
    reported as a finding. Churn is that number, named honestly and once.
    """
    graph = [_song("A", "X", 2, ["radio", "graph_artist"]),
             _song("NEW", "Y", 2, ["graph_artist", "graph_radio"])]
    native = [_song("A", "X", 1, ["radio"]), _song("OLD", "Z", 1, ["radio"])]
    graph_titles, native_titles = quality_check._titles(graph), quality_check._titles(native)

    ab = quality_check._ab(graph, native, graph_titles, native_titles, native_ceiling=3)
    assert ab["churn"] == 1
    assert len(graph_titles - native_titles) == len(native_titles - graph_titles)
    assert "added" not in ab and "displaced" not in ab


def test_corroboration_delta_is_what_answers_helping_or_diluting():
    """Same churn, opposite verdicts: the incoming song is better corroborated
    in one case and worse in the other. Churn alone cannot tell them apart."""
    native = [_song("A", "X", 2, ["radio", "artist"]), _song("OLD", "Z", 2, ["radio", "artist"])]
    native_titles = quality_check._titles(native)

    helped = [_song("A", "X", 2, ["radio", "artist"]),
              _song("NEW", "Y", 2, ["graph_artist", "graph_radio"])]
    diluted = [_song("A", "X", 2, ["radio", "artist"]),
               _song("NEW", "Y", 1, ["graph_artist"])]

    up = quality_check._ab(helped, native, quality_check._titles(helped), native_titles, 3)
    down = quality_check._ab(diluted, native, quality_check._titles(diluted), native_titles, 3)

    assert up["churn"] == down["churn"] == 1
    assert up["corroboration_delta"] == 0.0
    assert down["corroboration_delta"] == -0.5


def test_ab_delta_is_none_when_an_arm_returned_nothing():
    """Spotify's native arm returns nothing at all -- capabilities() is empty,
    so with the graph off there is no signal to gather. That must read as
    "not comparable", not as a delta of zero."""
    graph = [_song("A", "X", 2, ["graph_artist", "graph_radio"])]
    ab = quality_check._ab(graph, [], quality_check._titles(graph), set(), native_ceiling=0)
    assert ab["native_n"] == 0
    assert ab["corroboration_delta"] is None
    assert ab["churn"] == 1


def test_overlap_ignores_empty_sets_and_ranks_worst_first():
    pairs = quality_check._overlap_pairs({
        "a": {"1", "2", "3", "4"},
        "b": {"1", "2", "3", "9"},
        "c": {"1", "5", "6", "7"},
        "empty": set(),
    })
    assert pairs[0] == (0.75, "a", "b")
    assert not any("empty" in (p[1], p[2]) for p in pairs)


def test_source_ceiling_counts_graph_sources_only_when_the_graph_is_on():
    class _Fake:
        def capabilities(self):
            return {"radio", "related", "artist"}

    fake = _Fake()
    assert quality_check._source_ceiling(fake, graph_conn=object()) == 6
    assert quality_check._source_ceiling(fake, graph_conn=None) == 3


def test_source_ceiling_on_a_backend_with_no_native_signals():
    """Spotify declares none, so its ceiling is the graph alone -- and zero
    with the graph off, which is why the native arm there returns nothing and
    must be reported as such rather than as a quality collapse."""
    class _Restricted:
        def capabilities(self):
            return set()

    restricted = _Restricted()
    assert quality_check._source_ceiling(restricted, graph_conn=object()) == 3
    assert quality_check._source_ceiling(restricted, graph_conn=None) == 0
