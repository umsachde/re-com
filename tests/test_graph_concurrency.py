"""The graph cache crossing `gather_seeds`' thread pool.

This is the one shape a fake connection cannot model, and it is the shape that
shipped broken. `sqlite3.threadsafety` is 1: a connection used off its creating
thread raises ProgrammingError. `gather_seeds` fans seeds across a pool and
hands each worker the graph connection, so from the v6 merge until 2026-08-29
every multi-seed request with the graph enabled hit that -- and what it cost
depended entirely on the caller:

  recommend_from_playlist  skip_failures=False -- the error propagates, so the
                           tool failed outright, on both backends, for any
                           multi-track playlist.
  the mood path            skip_failures=True -- swallowed, leaving an empty
                           candidate pool and no error at all.

`recommend_from_song` shows neither symptom, because a single seed deliberately
stays on the calling thread. It was the only tool the v6 smoke tests exercised.

So every test here uses the *real* `graph_store` connection from the `graph_db`
fixture rather than a stub, and asserts against both callers -- a
`skip_failures=True` path and a `skip_failures=False` path over the same code
hide each other's bugs, one turning a defect into missing results and the other
into a crash.
"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import graph
import graph_store
import signals

from test_v6 import _FakeProvider, _deezer_for_ap_dhillon


_SEED_META = {
    "seed1": {"title": "Excuses", "artists": ["AP Dhillon"]},
    "seed2": {"title": "Excuses", "artists": ["AP Dhillon"]},
    "seed3": {"title": "Excuses", "artists": ["AP Dhillon"]},
}


def _graph_only_provider():
    """A backend with no native signals -- the graph is the only candidate source."""
    return _FakeProvider(capabilities=set())


# --- the platform assumption this all rests on ------------------------------


def test_a_connection_really_is_unusable_off_its_creating_thread(graph_db):
    # Pin the assumption rather than trusting it: if a future Python raises
    # threadsafety to 3, every test below would still pass while proving
    # nothing, and this one turns that into a visible failure instead.
    if sqlite3.threadsafety >= 3:
        pytest.skip("connections are shareable on this build; for_thread is moot")

    def use_it():
        graph_db.execute("SELECT 1").fetchone()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(sqlite3.ProgrammingError):
            pool.submit(use_it).result()


def test_for_thread_hands_back_a_usable_connection(graph_db):
    def use_it():
        conn = graph_store.for_thread(graph_db)
        return conn.execute("SELECT 1").fetchone()[0]

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(use_it).result() == 1


def test_for_thread_is_a_no_op_on_the_creating_thread(graph_db):
    # The single-seed path must stay exactly as it was -- no second connection.
    assert graph_store.for_thread(graph_db) is graph_db


def test_each_thread_gets_its_own_connection_and_reuses_it(graph_db):
    seen = {}

    def record():
        ident = threading.get_ident()
        first = graph_store.for_thread(graph_db)
        second = graph_store.for_thread(graph_db)
        assert first is second, "a thread must not reopen on every lookup"
        seen[ident] = id(first)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: record(), range(3)))

    assert len(seen) > 1, "the pool did not actually spread across threads"
    assert len(set(seen.values())) == len(seen), "threads shared a connection"


# --- the strict caller: recommend_from_playlist -----------------------------


def test_multi_seed_gather_does_not_raise_with_a_real_graph_connection(graph_db, monkeypatch):
    # The exact call recommend_from_playlist makes. Before graph_store.for_thread
    # this raised ProgrammingError and the tool failed outright.
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())

    per_seed = signals.gather_seeds(
        _graph_only_provider(),
        ["seed1", "seed2", "seed3"],
        skip_failures=False,
        graph_conn=graph_db,
        seed_meta=_SEED_META,
    )

    assert len(per_seed) == 3


def test_the_strict_path_still_returns_graph_candidates(graph_db, monkeypatch):
    # Not raising is only half of it: a gather that quietly found nothing would
    # also not raise. On a backend with no native signals the graph is the only
    # source, so an empty pool here means the same outage with a nicer face.
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())

    per_seed = signals.gather_seeds(
        _graph_only_provider(),
        ["seed1", "seed2", "seed3"],
        skip_failures=False,
        graph_conn=graph_db,
        seed_meta=_SEED_META,
    )

    assert all(found for found in per_seed), "a seed came back with no candidates"
    sources = {s for found in per_seed for c in found.values() for s in c["sources"]}
    assert sources == {"graph_artist", "graph_radio", "graph_related"}


# --- the forgiving caller: the mood path ------------------------------------


def test_the_mood_path_does_not_silently_lose_graph_candidates(graph_db, monkeypatch):
    # skip_failures=True turns the same defect into missing results and no
    # error, which is how this would have shipped a second time.
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())

    per_seed = signals.gather_seeds(
        _graph_only_provider(),
        ["seed1", "seed2", "seed3"],
        skip_failures=True,
        graph_conn=graph_db,
        seed_meta=_SEED_META,
    )

    assert len(per_seed) == 3, "a seed was dropped as a failure"
    assert all(found for found in per_seed)


def test_the_pooled_result_matches_the_single_threaded_one(graph_db, monkeypatch):
    # Threading must change the speed and nothing else. Same seed, on the
    # calling thread and through the pool, must find the same candidate keys.
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    provider = _graph_only_provider()

    on_this_thread = signals.gather_seeds(
        provider, ["seed1"], graph_conn=graph_db, seed_meta=_SEED_META
    )
    through_the_pool = signals.gather_seeds(
        provider, ["seed1", "seed2"], graph_conn=graph_db, seed_meta=_SEED_META
    )

    assert set(through_the_pool[0]) == set(on_this_thread[0])


def test_a_graph_failure_in_one_worker_does_not_sink_the_others(graph_db, monkeypatch):
    # The pre-existing skip_failures contract, re-asserted with a real
    # connection in play: a thread that dies must not take the pool with it.
    real_for_thread = graph_store.for_thread
    monkeypatch.setattr(graph, "_get", _deezer_for_ap_dhillon())
    exploded = threading.Event()

    def explode_once(conn):
        # The first worker to reach the graph dies the way a cross-thread use
        # would; every later one is served normally.
        if not exploded.is_set():
            exploded.set()
            raise sqlite3.ProgrammingError("simulated cross-thread use")
        return real_for_thread(conn)

    monkeypatch.setattr(graph_store, "for_thread", explode_once)

    per_seed = signals.gather_seeds(
        _graph_only_provider(),
        ["seed1", "seed2", "seed3"],
        skip_failures=True,
        graph_conn=graph_db,
        seed_meta=_SEED_META,
    )

    assert exploded.is_set(), "the failure never fired; the test proves nothing"
    assert len(per_seed) == 2, "one seed should be dropped, not all three"
    assert all(found for found in per_seed)
