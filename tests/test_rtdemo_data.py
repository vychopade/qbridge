"""Checks on data recorded from examples/rtdemo on the QNX 8.0 VirtualBox target.

rtdemo_* fixtures: three 10 s runs with the planted fault (PTHREAD_PRIO_NONE)
and three with priority inheritance restored, all pinned to CPU 1, plus the
per-cycle samples of the last run of each. vmjitter.json: timer and spin
checks on both virtual CPUs.

These tests check that rtdemo's measurements are internally consistent and
record what the target showed; they are not a timing guarantee.
"""

import csv
import io
import json

import pytest

DEADLINE_US = 10000
C_HOLD_US = 3000      # C holds the lock for 3 ms of CPU work


def _summary(fixture_text, name):
    return json.loads(fixture_text(f"{name}_summary.json"))


def _samples(fixture_text, name):
    rows = list(csv.DictReader(io.StringIO(fixture_text(f"{name}_samples.csv"))))
    return [{k: float(v) for k, v in r.items()} for r in rows]


@pytest.mark.parametrize("name", ["rtdemo", "rtdemo_fixed"])
def test_csv_agrees_with_summary(fixture_text, name):
    last = _summary(fixture_text, name)[-1]
    rows = _samples(fixture_text, name)
    assert len(rows) == last["cycles"]
    assert sum(int(r["missed"]) for r in rows) == last["misses"]
    assert max(r["response_us"] for r in rows) == pytest.approx(last["response"]["max_us"], abs=1)


@pytest.mark.parametrize("name", ["rtdemo", "rtdemo_fixed"])
def test_each_cycle_is_consistent(fixture_text, name):
    for r in _samples(fixture_text, name):
        # missed flag is exactly "response over the deadline"
        assert int(r["missed"]) == int(r["response_us"] > DEADLINE_US)
        # finishing takes at least the wait before starting plus the CPU used
        # (10 us allowed for the two clocks' rounding)
        assert r["response_us"] + 10 >= r["backlog_us"] + r["latency_us"] + r["exec_us"]
        # the lock wait happens inside the cycle
        assert r["response_us"] + 10 >= r["backlog_us"] + r["latency_us"] + r["lock_wait_us"]
        # A's own work is about 1 ms of CPU every cycle, whatever else happens
        assert 900 <= r["exec_us"] <= 2500


def test_fault_causes_large_repeatable_misses(fixture_text):
    runs = _summary(fixture_text, "rtdemo")
    assert len(runs) == 3
    for r in runs:
        assert r["lock_protocol"] == "PTHREAD_PRIO_NONE" and r["cpu"] == 1
        assert r["misses"] > 250                       # of 999 cycles
        assert r["response"]["max_us"] > 3 * DEADLINE_US
        assert r["lock_wait"]["max_us"] > 30000        # waits behind B's burst


def test_inheritance_removes_the_lock_wait(fixture_text):
    runs = _summary(fixture_text, "rtdemo_fixed")
    for r in runs:
        assert r["lock_protocol"] == "PTHREAD_PRIO_INHERIT"
        # with inheritance A waits at most for C's remaining critical section
        assert r["lock_wait"]["max_us"] <= C_HOLD_US + 500
        assert r["misses"] <= 2


def test_misses_left_after_fix_are_scheduling_latency_not_the_lock(fixture_text):
    """A miss in the fixed runs must not be blamed on the lock."""
    for r in _samples(fixture_text, "rtdemo_fixed"):
        if r["missed"]:
            assert r["lock_wait_us"] <= C_HOLD_US + 500
            assert r["latency_us"] + r["backlog_us"] > r["lock_wait_us"]


def test_vm_timer_check_shows_cpu0_is_unusable(fixture_text):
    checks = {(c["mode"], c["cpu"]): c for c in json.loads(fixture_text("vmjitter.json"))}
    t0, t1 = checks[("timer", 0)], checks[("timer", 1)]
    # CPU 0's timers fire tens of ms late with nothing else running ...
    assert t0["max_late_us"] > 20000 and t0["mean_late_us"] > 5000
    # ... while CPU 1 stays within a few ms (still not a hard guarantee)
    assert t1["max_late_us"] < 10000 and t1["mean_late_us"] < 1000
