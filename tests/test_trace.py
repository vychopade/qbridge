"""Kernel trace: parsing traceprinter output, checking findings against it, and the
capture tool. Fixtures are real traces from the QNX 8.0 target (rtdemo, Nov 22),
filtered to the rtdemo process plus every thread event on CPU 1 and gzipped
(tests/fixtures/traces/*.txt.gz); the .json beside each holds the diagnosis
findings from the same run."""

import copy
import gzip
import json
from pathlib import Path

import pytest

from qbridge import report, runs, tools, traceconfirm
from qbridge.parsers import kev
from qbridge.policy import Policy, PolicyError
from qbridge.target import Result, Target

FIX = Path(__file__).parent / "fixtures" / "traces"
KEV_OVERLOAD = Path(__file__).parents[1] / "examples" / "traces" / "overload.kev.gz"


def _load(name):
    text = gzip.decompress((FIX / f"{name}.txt.gz").read_bytes()).decode("utf-8")
    meta = json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))
    return text, kev.parse(text), meta


@pytest.fixture(scope="module")
def overload():
    return _load("overload_fault")


@pytest.fixture(scope="module")
def inversion():
    return _load("inversion")


@pytest.fixture(scope="module")
def fixed():
    return _load("overload_fixed")


# -- parser ------------------------------------------------------------------------

def test_parse_real_trace_names_and_states(overload):
    _, tr, meta = overload
    assert tr.cycles_per_sec == 3901841142
    assert 990 < tr.duration_ms < 1010                     # a 1 s capture
    a = tr.find_thread("A-control")
    assert list(a) == meta["threads"]["A-control"]
    assert tr.proc_names[a[0]].endswith("/rtdemo")
    tot = tr.state_totals(a)
    # The fault: A (priority 15) spends about a third of the second READY.
    assert tot["READY"] > 300 and tot.get("MUTEX", 0) == 0
    assert 990 < sum(tot.values()) < 1010


def test_parse_inversion_trace_has_mutex_waits(inversion):
    _, tr, _ = inversion
    a = tr.find_thread("A-control")
    assert tr.state_totals(a)["MUTEX"] > 300
    assert all(s.priority == 30 for s in tr.spans[a] if s.state != "CREATE")


def test_wrap_of_the_low_32_bits_is_detected():
    hdr = "  TRACE_CYCLES_PER_SEC:: 1000000000\n"
    text = hdr + "\n".join([
        "t:0x00000010 CPU:00 CONTROL         :TIME               msb:0x00000001 lsb(offset):0x00000010",
        "t:0xfffffff0 CPU:00 THREAD          :THRUNNING          pid:5 tid:1 priority:10 policy:1",
        "t:0x00000100 CPU:00 THREAD          :THREADY            pid:5 tid:1 priority:10 policy:1",
        "t:0x00000200 CPU:00 THREAD          :THRUNNING          pid:5 tid:1 priority:10 policy:1",
    ]) + "\n"
    tr = kev.parse(text)
    run = tr.spans[(5, 1)][0]
    assert run.state == "RUNNING"
    assert run.ms == pytest.approx((0x100 + 0x10) / 1e6, rel=1e-6)   # 0x110 cycles, not -4 s


def test_parse_rejects_non_trace_text():
    with pytest.raises(ValueError):
        kev.parse("hello\n")


def test_filter_drops_continuations_of_dropped_events():
    text = "\n".join([
        "  TRACE_CYCLES_PER_SEC:: 1000",
        "t:0x00000001 CPU:01 THREAD          :THRUNNING          pid:7 tid:1 priority:10 policy:1",
        "t:0x00000002 CPU:00 SYSTEM          :PATHMGR_OPEN      ",
        "                       pid:99",
        "                       tid:3",
        "t:0x00000003 CPU:00 PROCESS         :PROCTHREAD_NAME   ",
        "                       pid:7",
        "                       tid:1",
        "                      name:worker",
    ]) + "\n"
    out = kev.filter_text(text, {7}, cpu=1)
    assert "pid:99" not in out and "tid:3" not in out       # would have relabelled pid 7's event
    assert "name:worker" in out and "TRACE_CYCLES_PER_SEC" in out
    assert kev.parse(out).spans[(7, 1)][0].state == "RUNNING"


# -- confirming findings ---------------------------------------------------------------

def test_overload_finding_confirmed_by_its_trace(overload):
    _, tr, meta = overload
    f = copy.deepcopy(meta["findings"][0])
    assert f["rule"] == "cpu_preemption"
    r = traceconfirm.confirm(f, tr, "t1")
    assert r["status"] == "confirmed" and f["confirmed_by_trace"] is True
    assert all(s["supported"] for s in r["steps"]) and r["not_shown"] == []
    lw = r["steps"][-1]["longest_wait"]
    assert lw["ran_instead"][0]["thread"].endswith("'B-worker'") and lw["ran_instead"][0]["priority"] == 20
    lanes = [l["lane"] for l in r["timeline"]["lanes"]]
    assert lanes == ["task", "runner"]
    assert f["confidence"] == "high"                         # unchanged either way


def test_inversion_finding_confirmed_by_its_trace(inversion):
    _, tr, meta = inversion
    f = copy.deepcopy(meta["findings"][0])
    assert f["rule"] == "mutex_priority_inversion"
    r = traceconfirm.confirm(f, tr)
    assert r["status"] == "confirmed", r["not_shown"]
    assert len(r["steps"]) == 4
    assert any("owner" in x for x in r["limits"])            # says what a trace cannot show
    assert [l["lane"] for l in r["timeline"]["lanes"]] == ["waiter", "holder", "middle"]


def test_preemption_claim_not_confirmed_after_the_fix(overload, fixed):
    """The fault's finding, pointed at the same threads in the fixed run (A at 30):
    A is no longer kept waiting by B, so the trace must not confirm it."""
    _, tr, meta = fixed
    f = copy.deepcopy(overload[2]["findings"][0])
    pid = meta["rtdemo_pids"][0]
    f["subject"]["pid"] = f["subject"]["runner"]["pid"] = pid
    f["subject"]["task"]["tid"] = meta["threads"]["A-control"][1]
    f["subject"]["runner"]["tid"] = meta["threads"]["B-worker"][1]
    r = traceconfirm.confirm(f, tr)
    assert r["status"] == "not confirmed" and f["confirmed_by_trace"] is False
    assert r["not_shown"]
    assert f["confidence"] == "high"                          # confidence is left as it was


def test_preemption_claim_rejected_on_a_lock_wait(inversion, overload):
    """In the inversion run A waits on a mutex, not for the CPU: step one fails."""
    _, tr, meta = inversion
    f = copy.deepcopy(overload[2]["findings"][0])
    pid = meta["rtdemo_pids"][0]
    f["subject"]["pid"] = f["subject"]["runner"]["pid"] = pid
    f["subject"]["task"]["tid"] = meta["threads"]["A-control"][1]
    f["subject"]["runner"]["tid"] = meta["threads"]["B-worker"][1]
    r = traceconfirm.confirm(f, tr)
    assert r["status"] == "not confirmed"
    assert not r["steps"][0]["supported"]


def test_thread_mismatch_is_reported_not_guessed(inversion):
    _, tr, meta = inversion
    f = copy.deepcopy(meta["findings"][0])
    f["subject"]["holder"]["thread_name"] = "someone-else"
    r = traceconfirm.confirm(f, tr)
    assert r["status"] == "not confirmed"
    assert "not 'someone-else'" in r["steps"][0]["detail"]
    f = copy.deepcopy(meta["findings"][0])
    f["subject"]["pid"] = 4242
    f["subject"]["waiter"]["thread_name"] = "nobody"
    assert traceconfirm.confirm(f, tr)["status"] == "not confirmed"


def test_confirm_all_extends_summary(overload):
    _, tr, meta = overload
    res = {"summary": "1 finding(s).", "findings": copy.deepcopy(meta["findings"])}
    traceconfirm.confirm_all(res, tr, "tid")
    assert "1 of 1 finding(s) confirmed" in res["summary"] and res["trace_id"] == "tid"
    res = {"summary": "No priority inversion.", "findings": []}
    traceconfirm.confirm_all(res, tr, "tid")
    assert "no finding to check" in res["summary"]


# -- report ----------------------------------------------------------------------------

def test_report_trace_block(overload, fixed):
    _, tr, meta = overload
    f = copy.deepcopy(meta["findings"][0])
    traceconfirm.confirm(f, tr, "trace-1")
    html = report._trace_block(f)
    assert "Confirmed by kernel trace" in html and "<svg" in html and "trace-1" in html
    assert html.count('class="st-ready"') >= 1 and html.count('class="st-run"') >= 1
    _, tr2, meta2 = fixed
    g = copy.deepcopy(meta["findings"][0])
    g["subject"]["pid"] = g["subject"]["runner"]["pid"] = 1234567       # not in that trace
    traceconfirm.confirm(g, tr2)
    html = report._trace_block(g)
    assert "Not confirmed" in html and "Not shown:" in html
    assert report._trace_block({"title": "x"}) == ""


# -- the capture tool (fake target; the real capture was run on the target) ------------

class TraceTarget(Target):
    def __init__(self):
        super().__init__(host="192.0.2.10")
        self.calls = []

    def run(self, argv, timeout=60):
        self.calls.append(list(argv))
        return Result(" ".join(map(str, argv)), 0, "", "")

    def get(self, remote, local, timeout=300):
        # The raw trace of the same run as the overload fixture (examples/traces).
        self.calls.append(["get", remote])
        Path(local).write_bytes(gzip.decompress(KEV_OVERLOAD.read_bytes()))


def test_capture_trace_needs_write_and_uses_the_allowed_form(monkeypatch, overload):
    text = overload[0]
    t = TraceTarget()
    with pytest.raises(PolicyError):
        tools.capture_trace(tools.Session(t, Policy()), 1)
    assert t.calls == []

    def fake_printer(argv, **kw):
        Path(argv[argv.index("-o") + 1]).write_text(text, encoding="utf-8")
        return Result("", 0, "", "")
    monkeypatch.setattr(tools, "_traceprinter", lambda: "traceprinter")
    monkeypatch.setattr(tools.qnxenv, "load", lambda: {})     # no SDP needed for the test
    monkeypatch.setattr(tools.subprocess, "run", fake_printer)
    s = tools.Session(t, Policy(allow_write=True))
    trace_id, tr = tools.capture_trace(s, 99)                # clamped to the 5 s limit
    assert t.calls[0][:6] == ["tracelogger", "-w", "-n", "0", "-s", "5"]
    assert t.calls[1] == ["get", "/data/qbridge/qbridge_trace.kev"]
    d = runs.home() / "traces" / trace_id
    assert (d / "trace.kev").is_file() and (d / "trace.txt").is_file()
    assert tr.find_thread("A-control")
    assert tr.decoder == "qbridge native .kev decoder"     # traceprinter only adds trace.txt

    out = tools.trace(s, threads=["A-control", "nobody"])
    assert out["not_found"] == ["nobody"]
    a = out["threads"][0]
    assert a["state_ms"]["READY"] > 300
    assert a["longest_waits"][0]["ran_instead"][0]["thread"].endswith("'B-worker'")
