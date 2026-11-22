"""Two different faults, two rules, no cross-firing.

Fixtures were recorded on the QNX 8.0 target with `qbridge diagnose rtdemo`:
  scenario_overload_fault.json   -s overload -a 15  (A below the CPU-heavy B)
  scenario_overload_fixed.json   -s overload -a 30  (A above B)
  scenario_inversion.json        default            (mutex with PTHREAD_PRIO_NONE)
Each holds every snapshot taken (rtdemo's threads plus every RUNNING/READY
thread), the deadline report of the same run and the timing checks used.
"""

import copy
import json

import pytest

from qbridge import analysis


def _load(fixture_text, name):
    return json.loads(fixture_text(f"scenario_{name}.json"))


def _run_rules(d):
    return (analysis.mutex_inversion(d["snapshots"], d["deadline_report"]),
            analysis.cpu_preemption(d["snapshots"], d["deadline_report"], d["timing_checks"]))


def test_overload_fault_finds_cpu_preemption_only(fixture_text):
    d = _load(fixture_text, "overload_fault")
    assert d["args"] == ["-s", "overload", "-a", "15"]
    mutex, cpu = _run_rules(d)
    assert mutex == []                                   # no lock involved
    (f,) = cpu
    assert f["rule"] == "cpu_preemption" and f["confidence"] == "high"
    s = f["subject"]
    assert (s["task"]["thread_name"], s["task"]["priority"]) == ("A-control", 15)
    assert (s["runner"]["thread_name"], s["runner"]["priority"]) == ("B-worker", 20)
    assert f["observations"]["higher_running_while_ready"] >= 3
    assert any("0 with a lock wait" in e for e in f["evidence"])
    assert "no lock or code change" in f["suggested_fix"]


def test_overload_finding_is_honest_about_vm_noise(fixture_text):
    d = _load(fixture_text, "overload_fault")
    (f,) = analysis.cpu_preemption(d["snapshots"], d["deadline_report"], d["timing_checks"])
    tc1 = next(t for t in d["timing_checks"] if t["cpu"] == 1)
    worst_wait = max(d["deadline_report"]["parts_us"]["latency"]["max"],
                     d["deadline_report"]["parts_us"]["preempted"]["max"])
    if worst_wait > 2 * tc1["max_late_us"]:
        assert any("noise alone does not explain" in e for e in f["evidence"])
    else:
        assert any(e.startswith("Caution:") for e in f["evidence"])


def test_inversion_finds_mutex_only(fixture_text):
    d = _load(fixture_text, "inversion")
    mutex, cpu = _run_rules(d)
    assert cpu == []                                     # A (30) is never below a running thread
    (f,) = mutex
    assert f["confidence"] == "high" and f["subject"]["protocol"] == "PTHREAD_PRIO_NONE"


def test_earlier_mutex_recording_does_not_fire_cpu_rule(fixture_text):
    """Without a deadline report every READY thread is examined. In the inversion,
    C-logger (10) is READY while B-worker (20) runs, but C holds the mutex A (30)
    waits for: that is the mutex rule's case, not CPU overload."""
    snaps = json.loads(fixture_text("diagnose_snapshots_rtdemo.json"))["snapshots_kept"]
    assert analysis.cpu_preemption(snaps) == []
    assert analysis.mutex_inversion(snaps)[0]["confidence"] == "high"


def test_preempted_holder_is_left_to_the_mutex_rule():
    holder = _t(10, 2, "C", 10, "READY")
    holder["real_priority"] = 10
    waiter = _t(10, 1, "A", 30, "MUTEX")
    waiter["blocked_on"] = {"sync": "0x1", "mutex_owner_tid": 2, "mutex_owner_pid_bits_match": True}
    snaps = [[waiter, holder, _t(10, 3, "B", 20, "RUNNING")]]
    assert analysis.cpu_preemption(snaps) == []
    del waiter["blocked_on"]["mutex_owner_pid_bits_match"]     # holder unknown: no skip
    waiter["state"] = "NANOSLEEP"
    assert len(analysis.cpu_preemption(snaps)) == 1


def test_fixed_overload_finds_nothing(fixture_text):
    d = _load(fixture_text, "overload_fixed")
    assert d["args"] == ["-s", "overload", "-a", "30"]
    mutex, cpu = _run_rules(d)
    assert mutex == [] and cpu == []
    res = analysis.diagnose(d["snapshots"], d["deadline_report"], d["timing_checks"])
    assert "No priority inversion or CPU preemption" in res["summary"]
    assert "does not prove there is none" in res["summary"]


def test_live_results_match_rerun_of_rules(fixture_text):
    """What qbridge reported live is what the rules give on the recorded data."""
    for name, rule in (("overload_fault", "cpu_preemption"),
                       ("inversion", "mutex_priority_inversion")):
        d = _load(fixture_text, name)
        assert [f["rule"] for f in d["result"]["findings"]] == [rule]
    assert _load(fixture_text, "overload_fixed")["result"]["findings"] == []


# -- edge cases on small synthetic snapshots ---------------------------------------

def _t(pid, tid, name, prio, state, cpu=1, proc="app"):
    return {"pid": pid, "tid": tid, "name": proc, "thread_name": name, "priority": prio,
            "state": state, "last_cpu": cpu}


def test_kernel_threads_are_not_blamed():
    snaps = [[_t(10, 1, "ctl", 15, "READY"), _t(1, 5, "clock", 254, "RUNNING", proc="procnto")]]
    assert analysis.cpu_preemption(snaps) == []


def test_higher_thread_on_another_cpu_is_not_blamed():
    snaps = [[_t(10, 1, "ctl", 15, "READY", cpu=1), _t(20, 1, "hog", 40, "RUNNING", cpu=0)]]
    assert analysis.cpu_preemption(snaps) == []


def test_lower_priority_running_is_not_preemption():
    snaps = [[_t(10, 1, "ctl", 15, "READY"), _t(20, 1, "bg", 10, "RUNNING")]]
    assert analysis.cpu_preemption(snaps) == []


def test_single_observation_without_report_is_low_confidence():
    snaps = [[_t(10, 1, "ctl", 15, "READY"), _t(20, 1, "hog", 20, "RUNNING")],
             [_t(10, 1, "ctl", 15, "NANOSLEEP"), _t(20, 1, "hog", 20, "RUNNING")]]
    (f,) = analysis.cpu_preemption(snaps)
    assert f["confidence"] == "low"
    assert any("no deadline report" in u for u in f["could_not_determine"])
    assert any("no timing check" in u for u in f["could_not_determine"])


def test_lock_rooted_misses_do_not_raise_cpu_confidence(fixture_text):
    """A READY observation plus a lock-rooted deadline report must not look like overload."""
    report = copy.deepcopy(_load(fixture_text, "inversion")["deadline_report"])
    snaps = [[_t(10, 1, "A-control", 15, "READY"), _t(20, 1, "hog", 20, "RUNNING")]]
    (f,) = analysis.cpu_preemption(snaps, report)
    assert f["confidence"] == "low"
    assert any("with a lock wait" in e for e in f["evidence"])


def test_waits_far_above_vm_noise_raise_confidence():
    snaps = [[_t(10, 1, "A-control", 15, "READY"), _t(20, 1, "hog", 20, "RUNNING")]] * 3
    report = {"task": "A-control", "misses": 30, "miss_chains": 10,
              "chain_root_causes": {"latency": 10},
              "parts_us": {"lock_wait": {"max": 0.0}, "latency": {"max": 40000.0, "p99": 36000.0},
                           "preempted": {"max": 1000.0, "p99": 500.0}}}
    tc = [{"cpu": 1, "max_late_us": 2300.0}]
    (f,) = analysis.cpu_preemption(snaps, report, tc)
    assert f["confidence"] == "high"
    assert any("noise alone does not explain" in e for e in f["evidence"])
    assert not any("far above the rest" in e for e in f["evidence"])


def test_one_outlier_does_not_count_as_evidence():
    """Seen live: one 376.6 ms stall while the typical bad wait was ~35 ms. The
    comparison with VM noise must use the 99th percentile, and the outlier is
    named but not used."""
    snaps = [[_t(10, 1, "A-control", 15, "READY"), _t(20, 1, "hog", 20, "RUNNING")]] * 3
    report = {"task": "A-control", "misses": 30, "miss_chains": 10,
              "chain_root_causes": {"latency": 10},
              "parts_us": {"lock_wait": {"max": 0.0},
                           "latency": {"max": 376600.0, "p99": 35000.0},
                           "preempted": {"max": 1000.0, "p99": 500.0}}}
    tc = [{"cpu": 1, "max_late_us": 28400.0}]
    (f,) = analysis.cpu_preemption(snaps, report, tc)
    assert any(e.startswith("Caution:") and "35.0 ms; worst 376.6 ms" in e for e in f["evidence"])
    assert any("far above the rest" in e for e in f["evidence"])
    assert f["confidence"] == "high"          # held, usual, CPU-rooted misses: 3 points
