"""The raw-trace workflow end to end, as a judge or an assistant would use it with no
QNX installed: a .kev in, decoded natively, analysed, explained over MCP (live server
and `serve --replay`), and the outputs other tools already rely on unchanged."""

import gzip
import json
from pathlib import Path

import pytest

from qbridge import kevdeps, replaymode, server, tools
from qbridge.parsers import kev
from qbridge.policy import Policy
from qbridge.target import Target

ROOT = Path(__file__).resolve().parents[1]
KF = ROOT / "tests" / "fixtures" / "kev"
EX = ROOT / "examples" / "traces"


class NoTarget(Target):
    def run(self, argv, timeout=60):
        raise AssertionError(f"offline analysis ran a target command: {argv}")

    def get(self, *a, **k):
        raise AssertionError("offline analysis fetched a file from a target")


def _call(srv, name, **args):
    r = srv.call_tool(name, args)
    if r["isError"]:
        raise RuntimeError(r["content"][0]["text"])
    return r["structuredContent"]


@pytest.fixture
def live():
    return server.Server(tools.Session(NoTarget(host="192.0.2.1"), Policy()))


@pytest.fixture
def replay():
    s = tools.Session(NoTarget(host=None), Policy(allow_write=False))
    return server.Server(s, replaymode.tool_table(replaymode.ReplayState()), replaymode.INSTRUCTIONS)


@pytest.mark.parametrize("which", ["live", "replay"])
def test_mcp_trace_file_takes_a_raw_kev_and_explains_the_wait(which, request, tmp_path, monkeypatch):
    monkeypatch.setenv("QBRIDGE_HOME", str(tmp_path))
    srv = request.getfixturevalue(which)
    out = _call(srv, "qnx_trace_file", path=str(KF / "msgpass_fault.kev.gz"),
                threads=["A-control"], perfetto=True)
    assert out["decoder"] == "qbridge native .kev decoder"
    g = out["dependencies"]["groups"][0]
    assert g["classification"] == "confirmed_priority_inversion" and g["holder"].endswith("'S-server'")
    assert [e["event"] for e in g["worst"]["evidence"]] == ["COMM:SND_MESSAGE", "COMM:REC_MESSAGE",
                                                            "COMM:REPLY_MESSAGE"]
    assert Path(out["perfetto"]).is_file() and Path(out["html"]).is_file()


def test_the_mcp_description_tells_the_assistant_not_to_invent_holders():
    desc = server.TOOLS["qnx_trace_file"][0]
    assert "raw .kev" in desc and "no QNX SDP needed" in desc
    assert "never name a holder the result does not" in desc


def test_trace_file_keeps_every_key_it_had(tmp_path):
    """Scripts that read trace --file output keep working: new keys are only added."""
    s = tools.Session(NoTarget(host="192.0.2.1"), Policy())
    out = tools.trace_file(s, str(EX / "overload.txt.gz"), html_out=str(tmp_path / "o.html"))
    before = {"trace", "selected", "patterns", "periodic", "threads", "busiest", "not_found",
              "limits", "file", "html"}
    assert before <= set(out)
    assert set(out) - before == {"dependencies", "decoder"}
    assert set(out["trace"]) == {"duration_ms", "events", "cpus", "system", "date", "processes",
                                 "threads"}
    p = out["patterns"][0]
    assert {"pattern", "thread", "pid", "tid", "priority", "text"} <= set(p)


def test_kev_parse_api_is_unchanged():
    text = gzip.decompress((EX / "overload.txt.gz").read_bytes()).decode("utf-8")
    tr = kev.parse(text)
    assert isinstance(tr, kev.Trace) and tr.find_thread("A-control")
    assert tr.cycles_per_sec > 0 and tr.duration_ms > 900
    assert kev.Span("RUNNING", 0.0, 1.0, 0, 10).ms == 1.0


def test_a_semaphore_wait_is_tied_to_the_post():
    W, P, X = (3, 1), (3, 2), (3, 3)
    tr = kev.Trace(header={}, cycles_per_sec=10**9, duration_ms=50.0)
    tr.proc_names = {3: "/x/app"}
    tr.thread_names = {W: "W", P: "P", X: "X"}
    tr.spans[W] = [kev.Span("RUNNING", 0, 5, 0, 25), kev.Span("SEM", 5, 30, 0, 25),
                   kev.Span("RUNNING", 30, 50, 0, 25)]
    tr.spans[P] = [kev.Span("RUNNING", 0, 2, 1, 5), kev.Span("READY", 5, 28, 1, 5),
                   kev.Span("RUNNING", 28, 30, 1, 5), kev.Span("NANOSLEEP", 30, 50, 1, 5)]
    tr.spans[X] = [kev.Span("NANOSLEEP", 0, 5, 1, 15), kev.Span("RUNNING", 5, 28, 1, 15),
                   kev.Span("NANOSLEEP", 28, 50, 1, 15)]

    def link(t, cpu, ev, by):
        return {"t": t, "cpu": cpu, "cls": "KER_CALL", "ev": ev, "by": by, "off": None,
                "f": {"sync_p": "0x0000000000001000", "count": "0"}}
    tr.links = [link(4.9, 0, "SYNC_SEM_WAIT", W), link(29.9, 1, "SYNC_SEM_POST", P)]
    (w,) = kevdeps.waits(tr, W)
    r = kevdeps.analyse_wait(tr, W, w)
    assert r["kind"] == "semaphore" and r["holder"]["thread"] == "app[3] 'P'"
    assert r["classification"] == "confirmed_priority_inversion"       # X (15) between 5 and 25
    assert r["interference"]["middle"][0]["thread"] == "app[3] 'X'"
    assert r["holder"]["cpus"] == [1] and r["waiter_cpu"] == 0


def test_validation_report_in_the_repository_is_well_formed():
    rep = ROOT / "docs" / "validation" / "kev-validation.json"
    r = json.loads(rep.read_text(encoding="utf-8"))
    t = r["totals"]
    assert t["matched"] == t["events_traceprinter"] == t["events_qbridge"] > 100_000
    assert t["missing_in_qbridge"] == t["extra_in_qbridge"] == t["field_mismatch_events"] == 0
    assert t["timestamps_equal"] == t["timestamps_checked"]
    assert all("sha256" in f for f in r["files"])
    assert sum(f["events_traceprinter"] for f in r["files"]) == t["events_traceprinter"]
