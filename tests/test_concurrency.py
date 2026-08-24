"""Concurrent seed gathering (signals.gather_seeds).

Each seed costs ~4 sequential network round-trips and seeds don't depend on
each other, so they're gathered in parallel. These tests pin down the three
things that matters for: that it really is concurrent, that it stays bounded,
and that the failure semantics each call site relies on are preserved.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals  # noqa: E402
from ytmusic_client import YTMusicMCPError  # noqa: E402


class _SeedYT:
    """Returns one candidate per seed, named after it.

    `fail` raises a provider error, which _gather_seed_candidates absorbs per
    signal. `crash` raises something it doesn't expect -- the malformed-response
    class of bug (see the isinstance guard in _gather_seed_candidates) that
    gather_seeds' own skip_failures guard exists for.
    """

    def __init__(self, fail=(), crash=(), hook=None):
        self._fail = set(fail)
        self._crash = set(crash)
        self._hook = hook

    def get_watch_playlist(self, videoId, limit=25, radio=True):
        if self._hook:
            self._hook()
        if videoId in self._fail:
            raise YTMusicMCPError(f"{videoId} is dead")
        if videoId in self._crash:
            raise ValueError(f"{videoId} returned something unparseable")
        return {"tracks": [{"videoId": f"out-{videoId}", "title": videoId,
                            "artists": [{"name": "A"}]}]}


def _ids(per_seed):
    return [sorted(found) for found in per_seed]


# --- shape and ordering -----------------------------------------------------


def test_no_seeds_gathers_nothing():
    assert signals.gather_seeds(_SeedYT(), []) == []


def test_falsy_seed_ids_are_dropped():
    assert signals.gather_seeds(_SeedYT(), [None, ""]) == []


def test_results_keep_seed_order():
    per_seed = signals.gather_seeds(_SeedYT(), ["s1", "s2", "s3"])
    assert _ids(per_seed) == [["out-s1"], ["out-s2"], ["out-s3"]]


def test_a_single_seed_needs_no_pool():
    """The one-seed path stays on the calling thread -- recommend_from_song
    shouldn't pay for a thread pool it can't use."""
    caller = threading.current_thread().name
    seen = []
    yt = _SeedYT(hook=lambda: seen.append(threading.current_thread().name))

    signals.gather_seeds(yt, ["only"])
    assert seen == [caller]


# --- failure semantics ------------------------------------------------------


def test_a_seed_whose_signals_fail_yields_no_candidates_not_an_error():
    """Provider errors are already handled a level down, per signal -- the
    seed comes back empty rather than raising."""
    per_seed = signals.gather_seeds(_SeedYT(fail={"s2"}), ["s1", "s2"])
    assert _ids(per_seed) == [["out-s1"], []]


def test_a_crashing_seed_is_skipped_by_default():
    per_seed = signals.gather_seeds(_SeedYT(crash={"s2"}), ["s1", "s2", "s3"])
    assert _ids(per_seed) == [["out-s1"], ["out-s3"]]


def test_every_seed_crashing_returns_empty_rather_than_raising():
    assert signals.gather_seeds(_SeedYT(crash={"s1", "s2"}), ["s1", "s2"]) == []


def test_skip_failures_false_propagates_the_error():
    with pytest.raises(ValueError):
        signals.gather_seeds(_SeedYT(crash={"s2"}), ["s1", "s2"], skip_failures=False)


def test_skip_failures_false_propagates_on_the_single_seed_path():
    with pytest.raises(ValueError):
        signals.gather_seeds(_SeedYT(crash={"s1"}), ["s1"], skip_failures=False)


def test_a_crashing_single_seed_is_skipped_by_default():
    assert signals.gather_seeds(_SeedYT(crash={"s1"}), ["s1"]) == []


# --- actually concurrent ----------------------------------------------------


def test_seeds_really_do_run_at_the_same_time():
    """A barrier is the deterministic proof: it can only be cleared if every
    seed is in flight simultaneously. Run serially, this times out."""
    barrier = threading.Barrier(4, timeout=5)
    yt = _SeedYT(hook=barrier.wait)

    per_seed = signals.gather_seeds(yt, ["s1", "s2", "s3", "s4"])
    assert len(per_seed) == 4


def test_concurrency_is_capped_at_the_worker_limit(monkeypatch):
    """20 playlist seeds must not mean 20 simultaneous in-flight requests --
    that's the rate-limit exposure the cap exists to bound."""
    monkeypatch.setattr(signals, "SEED_WORKERS", 3)
    lock = threading.Lock()
    live, peak = [0], [0]

    def hook():
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.01)
        with lock:
            live[0] -= 1

    signals.gather_seeds(_SeedYT(hook=hook), [f"s{i}" for i in range(12)])
    assert peak[0] <= 3


def test_max_workers_overrides_the_default(monkeypatch):
    monkeypatch.setattr(signals, "SEED_WORKERS", 8)
    barrier = threading.Barrier(2, timeout=5)
    yt = _SeedYT(hook=barrier.wait)

    # Only clears if exactly-2-at-a-time is honoured; a third concurrent call
    # would make the barrier trip early, and a serial run would time out.
    per_seed = signals.gather_seeds(yt, ["s1", "s2", "s3", "s4"], max_workers=2)
    assert len(per_seed) == 4


def test_a_zero_worker_setting_still_runs(monkeypatch):
    """Guard against a misconfigured RECOM_SEED_WORKERS taking the tool down."""
    monkeypatch.setattr(signals, "SEED_WORKERS", 0)
    per_seed = signals.gather_seeds(_SeedYT(), ["s1", "s2"])
    assert _ids(per_seed) == [["out-s1"], ["out-s2"]]


def test_gathering_is_faster_than_the_serial_sum():
    yt = _SeedYT(hook=lambda: time.sleep(0.1))
    started = time.monotonic()
    signals.gather_seeds(yt, [f"s{i}" for i in range(6)])
    elapsed = time.monotonic() - started

    # Six 0.1s seeds: ~0.6s serial, ~0.1s concurrent. A generous ceiling keeps
    # this from flaking on a loaded machine while still failing if it's serial.
    assert elapsed < 0.4
