"""Who waited for whom (kevdeps.py): mutex, message-passing and condition-variable waits
tied to the thread that ended them, from kernel-call and communication events.

Real traces: tests/fixtures/kev (rtdemo's scenarios recorded with every class on, each
fault beside its fix; see the README there). The fixes are the false-positive checks:
the same program and load, with the inversion removed. Synthetic traces cover chains,
cross-CPU interference, missing evidence and dropped events.
"""

import gzip
from pathlib import Path

import pytest

from qbridge import kevdeps, tools, traceview
from qbridge.parsers import kev
from qbridge.policy import Policy
from qbridge.target import Target

KF = Path(__file__).parent / "fixtures" / "kev"
EX = Path(__file__).parents[1] / "examples" / "traces"


def _tr(name):
    return kev.parse_kev(gzip.decompress((KF / f"{name}.kev.gz").read_bytes()))


def _groups(tr, thread="A-control"):
    k = tr.find_thread(thread)
    return kevdeps.analyse(tr, [k])["groups"]


def _confirmed(groups):
    return [g for g in groups if g["classification"] == "confirmed_priority_inversion"]


# -- real traces: each fault is confirmed, its fix is not ---------------------------------------

def test_mutex_inversion_is_confirmed_with_the_holder_named_by_the_trace():
    tr = _tr("inversion_fault")
    (g,) = _confirmed(_groups(tr))
    w = g["worst"]
    assert g["kind"] == "mutex" and g["holder"].endswith("'C-logger'") and g["waits"] >= 2
    assert w["waiter_priority"] == 30 and w["holder"]["own_priority"] == 10
    assert w["holder"]["same_process"] and w["dependency"] == "proven by trace events"
    roles = [e["role"] for e in w["evidence"]]
    assert roles == ["waiter's SyncMutexLock", "holder's SyncMutexUnlock of the same mutex"]
    lock, unlock = w["evidence"]
    assert lock["thread"].endswith("'A-control'") and unlock["thread"].endswith("'C-logger'")
    assert lock["fields"]["sync_p"] == unlock["fields"]["sync_p"]
    assert "owner value in the waiter's call equals the unlocker's" in w["proof"]
    assert all(isinstance(e["file_offset"], int) for e in w["evidence"])
    mid = w["interference"]["middle"][0]
    assert mid["thread"].endswith("'B-worker'") and mid["priority"] == 20 and mid["ms"] > 25
    assert mid["cpu"] in w["holder"]["cpus"]
    assert w["cpus_during_wait"]["1"][0]["thread"].endswith("'B-worker'")


def test_mutex_fix_shows_priority_inheritance_not_an_inversion():
    tr = _tr("inversion_fixed")
    gs = _groups(tr)
    assert gs and not _confirmed(gs)
    g = next(g for g in gs if g["holder"] and g["holder"].endswith("'C-logger'"))
    assert g["classification"] == "no_inversion"
    assert "ran at priority 30" in g["worst"]["inheritance"] and "its own priority is 10" in g["worst"]["inheritance"]


def test_message_passing_inversion_across_processes_is_confirmed():
    tr = _tr("msgpass_fault")
    (g,) = _confirmed(_groups(tr))
    w = g["worst"]
    assert g["kind"] == "message" and g["holder"].endswith("'S-server'")
    assert not w["holder"]["same_process"] and w["holder"]["pid"] != w["pid"]
    assert [e["event"] for e in w["evidence"]] == ["COMM:SND_MESSAGE", "COMM:REC_MESSAGE",
                                                   "COMM:REPLY_MESSAGE"]
    snd, rec, rep = w["evidence"]
    assert snd["fields"]["rcvid"] == rec["fields"]["rcvid"]
    assert int(snd["fields"]["pid"]) == w["holder"]["pid"]
    assert rep["fields"] == {"tid": str(w["tid"]), "pid": str(w["pid"])}
    assert w["interference"]["middle"][0]["thread"].endswith("'B-worker'")
    assert w["interference"]["middle_priority_ms"] > 30


def test_message_passing_fix_is_not_an_inversion():
    gs = _groups(_tr("msgpass_fixed"))
    assert gs and not _confirmed(gs)
    assert {g["classification"] for g in gs if g["holder"]} == {"no_inversion"}
    assert "ran at priority 30, the waiter's" in gs[0]["worst"]["reason"]


def test_condition_variable_inversion_is_confirmed_by_who_signalled():
    tr = _tr("condvar_fault")
    (g,) = _confirmed(_groups(tr))
    w = g["worst"]
    assert g["kind"] == "condvar" and g["holder"].endswith("'P-producer'")
    assert [e["event"] for e in w["evidence"]] == ["KER_CALL:SYNC_CONDVAR_WAIT",
                                                   "KER_CALL:SYNC_CONDVAR_SIGNAL"]
    assert w["evidence"][0]["fields"]["sync_p"] == w["evidence"][1]["fields"]["sync_p"]
    assert "has no owner" in w["proof"]


def test_condition_variable_fix_is_not_an_inversion():
    gs = _groups(_tr("condvar_fixed"))
    assert gs and not _confirmed(gs)


def test_cpu_overload_is_not_reported_as_an_inversion():
    tr = _tr("overload_fast")
    d = kevdeps.analyse(tr)
    assert not _confirmed(d["groups"])


@pytest.mark.parametrize("name", ["inversion_fault", "inversion_fixed", "msgpass_fault",
                                  "msgpass_fixed", "condvar_fault", "condvar_fixed"])
def test_every_holder_comes_with_evidence(name):
    for g in kevdeps.analyse(_tr(name))["groups"]:
        w = g["worst"]
        if g["holder"]:
            assert w["dependency"] == "proven by trace events" and len(w["evidence"]) >= 1
            assert w["proof"]
        else:
            assert g["classification"] == "insufficient_evidence" and w["reason"]


def test_waits_cut_by_the_end_of_the_trace():
    # No signal in the trace: nothing names the holder.
    tr = _tr("condvar_fault")
    a = tr.find_thread("A-control")
    r = kevdeps.analyse_wait(tr, a, kevdeps.waits(tr, a)[-1])
    assert r["cut_by_trace"] == "end" and r["classification"] == "insufficient_evidence"
    assert "still waiting when the trace ended" in r["reason"]
    # The holder is named by its owner value (its own unlock earlier in the trace): the
    # recorded part is measured and says so.
    tr = _tr("inversion_fault")
    a = tr.find_thread("A-control")
    r = kevdeps.analyse_wait(tr, a, kevdeps.waits(tr, a)[-1])
    assert r["cut_by_trace"] == "end" and r["holder"]["thread"].endswith("'C-logger'")
    assert "elsewhere in the trace" in r["proof"] and "past the end of the trace" in r["text"]


def test_dropped_events_downgrade_a_confirmed_inversion():
    tr = _tr("inversion_fault")
    a = tr.find_thread("A-control")
    w = max(kevdeps.waits(tr, a), key=lambda w: w["end"] - w["start"])
    assert kevdeps.analyse_wait(tr, a, w)["classification"] == "confirmed_priority_inversion"
    tr.drops.append({"t": w["start"] + 5, "cpu": 1, "count": 100})
    r = kevdeps.analyse_wait(tr, a, w)
    assert r["classification"] == "probable_scheduling_interference"
    assert r["dropped_events"][0]["count"] == 100 and "dropped" in r["reason"]


def test_traces_without_kernel_calls_tie_no_wait_to_a_holder():
    tr = traceview.load_trace(EX / "msgpass.kev.gz")
    assert not tr.links
    d = kevdeps.analyse(tr)
    assert d["groups"] == [] and "thread states only" in d["source"]


# -- synthetic traces: chains, CPUs, missing evidence ------------------------------------------

def _span(st, a, b, cpu, prio):
    return kev.Span(st, a, b, cpu, prio)


def _link(t, cpu, cls, ev, by, **f):
    return {"t": t, "cpu": cpu, "cls": cls, "ev": ev, "f": {k: str(v) for k, v in f.items()},
            "by": by, "off": None}


def _chain_trace():
    """A (30, CPU 1) waits on a mutex held by C (10, CPU 0); C is itself REPLY-blocked on a
    server S (10, CPU 0), which B (20, CPU 0) keeps off the CPU. Then C unlocks."""
    A, B, C, S = (5, 1), (5, 2), (5, 3), (9, 1)
    tr = kev.Trace(header={}, cycles_per_sec=10**9, duration_ms=100.0)
    tr.proc_names = {5: "/x/app", 9: "/x/server"}
    tr.thread_names = {A: "A", B: "B", C: "C", S: "S"}
    tr.spans[A] = [_span("RUNNING", 0, 10, 1, 30), _span("MUTEX", 10, 60, 1, 30),
                   _span("RUNNING", 60, 100, 1, 30)]
    tr.spans[C] = [_span("RUNNING", 0, 5, 0, 10), _span("SEND", 5, 6, 0, 10),
                   _span("REPLY", 6, 55, 0, 10), _span("RUNNING", 55, 60, 0, 10),
                   _span("READY", 60, 100, 0, 10)]
    tr.spans[S] = [_span("RECEIVE", 0, 6, 0, 10), _span("READY", 6, 50, 0, 10),
                   _span("RUNNING", 50, 55, 0, 10), _span("RECEIVE", 55, 100, 0, 10)]
    tr.spans[B] = [_span("NANOSLEEP", 0, 6, 0, 20), _span("RUNNING", 6, 50, 0, 20),
                   _span("NANOSLEEP", 50, 100, 0, 20)]
    tr.links = [
        _link(5.5, 0, "COMM", "SND_MESSAGE", C, rcvid="0x00010001", pid=9),
        _link(9.9, 1, "KER_CALL", "SYNC_MUTEX_LOCK", A, sync_p="0x10", count=-2147483648,
              owner=0x00050004),
        _link(50.1, 0, "COMM", "REC_MESSAGE", S, rcvid="0x00010001", pid=9),
        _link(54.9, 0, "COMM", "REPLY_MESSAGE", S, tid=3, pid=5),
        _link(59.9, 0, "KER_CALL", "SYNC_MUTEX_UNLOCK", C, sync_p="0x10", count=-2147483648,
              owner=0x80050004),
    ]
    return tr, A, C, S


def test_a_dependency_chain_across_processes_and_cpus():
    tr, A, C, S = _chain_trace()
    (w,) = kevdeps.waits(tr, A)
    r = kevdeps.analyse_wait(tr, A, w)
    assert r["holder"]["thread"] == "app[5] 'C'" and r["holder"]["cpus"] == [0]
    assert r["waiter_cpu"] == 1                       # the waiter and holder on different CPUs
    assert "owner value in the waiter's call equals the unlocker's (0x00050004)" in r["proof"]
    (inner,) = r["chain"]
    assert inner["kind"] == "message" and inner["holder"]["thread"] == "server[9] 'S'"
    # S (10) held off by B (20) while A (30) waited at the start of the chain.
    assert inner["waiter_priority"] == 10 and inner["judged_at_priority"] == 30
    assert inner["classification"] == "confirmed_priority_inversion"
    assert inner["interference"]["middle"][0] == {"thread": "app[5] 'B'", "priority": 20,
                                                  "cpu": 0, "ms": 44.0}
    assert r["classification"] == "confirmed_priority_inversion"
    assert r["reason"].startswith("through a chain: app[5] 'C' was itself waiting for server[9] 'S'")


def test_no_holder_without_an_event_that_names_one():
    tr, A, C, S = _chain_trace()
    tr.links = [l for l in tr.links if l["ev"] != "SYNC_MUTEX_UNLOCK"]
    r = kevdeps.analyse_wait(tr, A, kevdeps.waits(tr, A)[0])
    assert r["classification"] == "insufficient_evidence" and "holder" not in r
    assert "release of this mutex is not in the trace" in r["reason"]


def test_owner_value_seen_in_an_unlock_elsewhere_identifies_the_holder():
    tr, A, C, S = _chain_trace()
    unlock = next(l for l in tr.links if l["ev"] == "SYNC_MUTEX_UNLOCK")
    unlock["t"] = 90.0                                # released after the wait (not logged as such)
    r = kevdeps.analyse_wait(tr, A, kevdeps.waits(tr, A)[0])
    assert r["holder"]["thread"] == "app[5] 'C'"
    assert "elsewhere in the trace" in r["proof"]


def test_interference_at_or_above_the_waiters_priority_is_not_an_inversion():
    tr, A, C, S = _chain_trace()
    B = (5, 2)
    tr.spans[B] = [_span(s.state, s.start, s.end, s.cpu, 35) for s in tr.spans[B]]
    (w,) = kevdeps.waits(tr, A)
    r = kevdeps.analyse_wait(tr, A, w)
    inner = r["chain"][0]
    assert inner["classification"] == "probable_scheduling_interference"
    assert "not a priority inversion" in inner["reason"]
    assert r["classification"] != "confirmed_priority_inversion"


def test_short_runs_inside_a_wait_do_not_split_it():
    tr = _tr("inversion_fault")
    a = tr.find_thread("A-control")
    ws = kevdeps.waits(tr, a)
    assert all(w["end"] - w["start"] >= kevdeps.MIN_WAIT_MS for w in ws)
    starts = [w["start"] for w in ws]
    assert starts == sorted(starts)


# -- in the reports ----------------------------------------------------------------------------

def test_trace_file_reports_dependencies_html_and_perfetto(tmp_path):
    s = tools.Session(Target(host="192.0.2.1"), Policy())
    out = tools.trace_file(s, str(KF / "inversion_fault.kev.gz"), process="rtdemo",
                           html_out=str(tmp_path / "i.html"), perfetto_out=str(tmp_path / "i.json"))
    d = out["dependencies"]
    assert d["waits_by_classification"]["confirmed_priority_inversion"] >= 2
    p = next(p for p in out["patterns"] if p["pattern"] == "possible_priority_inversion")
    assert p["classification"] == "confirmed_priority_inversion" and "cannot_tell" not in p
    assert "Confirmed by kernel calls in this trace" in p["text"]
    page = (tmp_path / "i.html").read_text(encoding="utf-8")
    assert "Who waited for whom" in page and "confirmed priority inversion" in page
    import json
    ev = json.loads((tmp_path / "i.json").read_text(encoding="utf-8"))["traceEvents"]
    starts = [e for e in ev if e.get("cat") == "dependency" and e["ph"] == "s"]
    ends = [e for e in ev if e.get("cat") == "dependency" and e["ph"] == "f"]
    assert starts and len(starts) == len(ends) and {e["id"] for e in starts} == {e["id"] for e in ends}
    marker = next(e for e in ev if e.get("cat") == "dependency" and e["ph"] == "i")
    assert marker["name"].startswith("confirmed priority inversion: waits for")
    assert any("SYNC_MUTEX_UNLOCK" in x for x in marker["args"]["evidence"])


def test_hints_from_thread_states_alone_are_marked_probable(tmp_path):
    s = tools.Session(Target(host="192.0.2.1"), Policy())
    out = tools.trace_file(s, str(EX / "inversion.txt.gz"), html_out=str(tmp_path / "x.html"))
    p = next(p for p in out["patterns"] if p["pattern"] == "possible_priority_inversion")
    assert p["classification"] == "probable_scheduling_interference" and p["cannot_tell"]
    assert out["dependencies"]["groups"] == []
