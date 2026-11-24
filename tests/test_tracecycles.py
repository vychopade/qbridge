"""Deadline statistics rebuilt from a kernel trace alone (tracecycles.py).

The main check is against ground truth: on Nov 23 three 2-second traces were
recorded on the target while rtdemo wrote its own per-cycle log in the same run
(tests/fixtures/tracecycles). Every cycle rebuilt from the trace must agree with
rtdemo's log on whether it missed its deadline."""

import gzip
import statistics as st
from pathlib import Path

import pytest

from qbridge import analysis, deadlines, tracecycles, traceview
from qbridge.parsers import kev

ROOT = Path(__file__).resolve().parents[1]
VAL = ROOT / "tests" / "fixtures" / "tracecycles"
EX = ROOT / "examples" / "traces"


def _trace(path):
    return kev.parse(traceview.load_text(path))


def _a(tr):
    return next(k for k, n in tr.thread_names.items() if n == "A-control" and k in tr.spans)


@pytest.mark.parametrize("name,expect_misses", [("inversion", 61), ("overload", 60), ("msgpass", 152)])
def test_every_cycle_agrees_with_rtdemos_own_log(name, expect_misses):
    tr = _trace(VAL / f"{name}.trace.txt.gz")
    c = tracecycles.cycles(tr, _a(tr), keep_samples=True)
    ts = c["samples"]
    own = deadlines.parse_samples_csv(gzip.decompress((VAL / f"{name}.samples.csv.gz").read_bytes())
                                      .decode("utf-8"))
    # The trace covers 2 s of a 6 s run: find where it sits in rtdemo's log.
    best = min(range(len(own) - len(ts) + 1),
               key=lambda k: st.mean(abs(t.response - own[i + k].response) for i, t in enumerate(ts)))
    pairs = [(t, own[i + best]) for i, t in enumerate(ts)]
    dl = 10_000
    assert all((t.response > dl) == (o.response > dl) for t, o in pairs)
    assert sum(t.response > dl for t, _ in pairs) == expect_misses
    diffs = sorted(abs(t.response - o.response) for t, o in pairs)
    assert diffs[len(diffs) // 2] < 300                # median under 0.3 ms
    assert c["estimated_cycles"] > 0                   # boundaries inside overruns were estimated


def test_period_and_causes_on_the_full_sample_traces():
    tr = _trace(EX / "msgpass.txt.gz")
    per = {c["thread"].split("'")[1]: c for c in tracecycles.periodic_threads(tr)
           if "'" in c["thread"]}
    assert per["A-control"]["period_ms"] == pytest.approx(10.0, abs=0.01)
    assert per["B-worker"]["period_ms"] == pytest.approx(100.0, abs=0.05)
    assert per["A-control"]["report"]["chain_root_causes"] == {"server_wait": 10}
    assert per["A-control"]["deadline_source"] == "taken as the period"
    tr = _trace(EX / "overload.txt.gz")
    a = tracecycles.cycles(tr, _a(tr))
    assert set(a["report"]["chain_root_causes"]) == {"latency"}


def test_given_period_and_deadline_and_a_shell_is_not_periodic():
    tr = _trace(EX / "overload.txt.gz")
    a = _a(tr)
    loose = tracecycles.cycles(tr, a, 10.0, 12.0)
    tight = tracecycles.cycles(tr, a, 10.0, 8.0)
    assert loose["deadline_source"] == "given" and loose["period_source"] == "given"
    assert tight["report"]["misses"] > loose["report"]["misses"]
    shell = next(k for k, p in tr.proc_names.items() if p.endswith("ksh"))
    assert tracecycles.cycles(tr, next(k for k in tr.spans if k[0] == shell)) is None


def test_analyse_reports_periodic_threads_and_needs_a_thread_for_a_period():
    tr = _trace(EX / "overload.txt.gz")
    r = traceview.analyse(tr)
    a = next(c for c in r["periodic"] if c["thread"].endswith("'A-control'"))
    assert a["report"]["misses"] > 0
    assert a["findings"] == []             # the preemption hint explains these misses
    with pytest.raises(ValueError, match="--thread"):
        traceview.analyse(tr, period_ms=10)
    page = traceview.html(r, tr, "overload.txt.gz")
    assert "Periodic threads and their deadlines" in page


def _synthetic(late_cycles=(10, 20), late_ms=15, cycles=30, long_cycles=(), long_ms=25):
    """A 10 ms task at priority 30 that runs 1 ms per cycle and sleeps in between; in
    `late_cycles` it wakes `late_ms` late, and in `long_cycles` its work takes `long_ms`,
    with nothing else running. After either it runs the cycles it fell behind on back to
    back, as a task on an absolute periodic timer does."""
    us = []
    t_end = 0
    k = 0
    while k < cycles:
        rel = 1000 + k * 10_000
        wake = rel + (late_ms * 1000 if k in late_cycles else 0)
        us.append((wake, "THREADY"))
        us.append((wake + 5, "THRUNNING"))
        t = wake + 5 + (long_ms * 1000 if k in long_cycles else 1000)
        n = 1
        while rel + n * 10_000 <= t:                # releases passed: run them back to back
            t += 1000
            n += 1
        us.append((t, "THNANOSLEEP"))
        k += n
        t_end = t
    us.append((t_end + 20_000, "THREADY"))
    lines = ["  TRACE_CYCLES_PER_SEC:: 1000000", " -- KERNEL EVENTS -- ",
             "t:0x00000000 CPU:01 THREAD          :THNANOSLEEP        pid:7 tid:1 priority:30 policy:1",
             "t:0x00000000 CPU:01 PROCESS         :PROCCREATE_NAME", "                          pid:7",
             "                          name:data/app",
             "t:0x00000000 CPU:01 PROCESS         :PROCTHREAD_NAME", "                          pid:7",
             "                          tid:1", "                          name:ctl"]
    lines += [f"t:0x{t:08x} CPU:01 THREAD          :{ev:<18} pid:7 tid:1 priority:30 policy:1"
              for t, ev in us]
    return "\n".join(lines) + "\n"


def test_late_wakeups_found_in_a_trace_of_an_uninstrumented_program():
    tr = kev.parse(_synthetic())
    c = tracecycles.cycles(tr, (7, 1), keep_samples=True)
    assert c["period_ms"] == pytest.approx(10.0, abs=0.01)
    r = c["report"]
    assert r["misses"] == 2 and r["chain_root_causes"] == {"latency": 2}
    late = [s for s in c["samples"] if s.response > 10_000]
    assert all(14_900 < s.latency < 15_100 for s in late)      # slept 15 ms past the release
    caught_up = [s for s in c["samples"] if s.backlog > 0]
    assert len(caught_up) == 2 and all(s.response < 10_000 for s in caught_up)
    res = traceview.analyse(tr)
    (p,) = res["periodic"]
    (f,) = p["findings"]
    assert f["rule"] == analysis.RULE_LATE_WAKEUP
    assert any("rebuilt from the kernel trace" in u for u in f["could_not_determine"])


def test_trace_check_confirms_a_late_wakeup_step_by_step():
    from qbridge import traceconfirm
    tr = kev.parse(_synthetic())
    (f,) = traceview.analyse(tr)["periodic"][0]["findings"]
    c = traceconfirm.confirm(f, tr, "synthetic")
    assert c["status"] == "confirmed" and len(c["steps"]) == 4
    assert "still asleep after its release" in c["steps"][3]["claim"]
    assert c["timeline"] and c["timeline"]["lanes"][0]["lane"] == "task"


def test_overrun_found_and_confirmed_in_a_trace():
    from qbridge import traceconfirm
    tr = kev.parse(_synthetic(late_cycles=(), long_cycles=(12,), long_ms=25))
    p = traceview.analyse(tr)["periodic"][0]
    assert p["report"]["chain_root_causes"] == {"exec": 1}
    (f,) = p["findings"]
    assert f["rule"] == analysis.RULE_OVERRUN
    c = traceconfirm.confirm(f, tr, "synthetic")
    assert c["status"] == "confirmed", c["not_shown"]
    assert any("virtual machine" in x for x in c["limits"])


def test_trace_window_without_late_starts_does_not_confirm():
    """The fixed overload run's finding (late wake-ups of 10-16 ms) against a trace from
    another run whose window has none that large: not shown, not disproved."""
    import json
    from qbridge import traceconfirm
    d = json.loads((ROOT / "tests" / "fixtures" / "scenario_overload_fixed.json").read_text(encoding="utf-8"))
    res = analysis.diagnose(d["snapshots"], d["deadline_report"], d["timing_checks"])
    tr = _trace(ROOT / "tests" / "fixtures" / "traces" / "overload_fixed.txt.gz")
    for f in res["findings"]:
        f["subject"]["pid"] = None                       # another run: match by name
    traceconfirm.confirm_all(res, tr, "t")
    late = next(f for f in res["findings"] if f["rule"] == analysis.RULE_LATE_WAKEUP)
    assert late["trace"]["status"] == "not confirmed"
    assert late["trace"]["steps"][0]["supported"]          # cycles were rebuilt
    assert "0 of 2 finding(s) confirmed" in res["summary"]
