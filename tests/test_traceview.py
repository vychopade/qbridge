"""Offline analysis of a recorded kernel trace (qbridge trace --file, qnx_trace_file).

Inputs are real traces from the QNX 8.0 target: the full one-second captures
in examples/traces/ (every process), and the filtered fixtures in
tests/fixtures/traces/ (rtdemo plus CPU 1). No target is used."""

import gzip
import json
from pathlib import Path

import pytest

from qbridge import audit, cli, server, tools, traceview
from qbridge.parsers import kev
from qbridge.policy import Policy
from qbridge.target import Target

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples" / "traces"
FIX = ROOT / "tests" / "fixtures" / "traces"


class NoTarget(Target):
    """Fails loudly if offline analysis ever touches the target."""

    def run(self, argv, timeout=60):
        raise AssertionError(f"offline analysis ran a target command: {argv}")

    def get(self, *a, **k):
        raise AssertionError("offline analysis fetched a file from the target")


def _analyse(path, **kw):
    tr = kev.parse(traceview.load_text(path))
    return tr, traceview.analyse(tr, **kw)


def _names(r, pattern):
    return [p for p in r["patterns"] if p["pattern"] == pattern]


# -- patterns on the full sample traces --------------------------------------------------

def test_mutex_inversion_points_at_the_logger_held_off_by_the_worker():
    _, r = _analyse(EX / "inversion.txt.gz")
    (inv,) = _names(r, "possible_priority_inversion")
    assert inv["thread"].endswith("'A-control'") and inv["priority"] == [30]
    assert inv["blocked_in"] == ["MUTEX"] and inv["blocked_ms"] > 300
    assert inv["lower"]["thread"].endswith("'C-logger'") and inv["lower"]["priority"] == 10
    assert inv["middle"][0]["thread"].endswith("'B-worker'") and inv["middle"][0]["priority"] == 20
    assert "candidate, not a proven holder" in inv["cannot_tell"]
    # The logger's own wait for the CPU is part of the inversion, not a second story.
    assert not any(p["thread"].endswith("'C-logger'") for p in _names(r, "cpu_preemption"))
    assert r["patterns"][0] is inv                      # highest priority first


def test_message_passing_inversion_points_at_the_server():
    _, r = _analyse(EX / "msgpass.txt.gz")
    (inv,) = _names(r, "possible_priority_inversion")
    assert inv["thread"].endswith("'A-control'")
    assert set(inv["blocked_in"]) <= {"SEND", "REPLY"} and "REPLY" in inv["blocked_in"]
    assert inv["lower"]["thread"].endswith("'S-server'") and inv["lower"]["priority"] == 10
    assert inv["middle"][0]["thread"].endswith("'B-worker'")
    assert all(m["ms"] >= 0.1 * inv["middle"][0]["ms"] for m in inv["middle"])


def test_fixed_message_passing_has_no_inversion():
    _, r = _analyse(EX / "msgpass_fixed.txt.gz")
    assert not _names(r, "possible_priority_inversion")
    # The worker now waits behind the server running at its client's priority.
    (pre,) = _names(r, "cpu_preemption")
    assert pre["thread"].endswith("'B-worker'")
    assert pre["ran_instead"][0]["thread"].endswith("'S-server'")
    assert pre["ran_instead"][0]["priority"] == 30


def test_overload_is_cpu_preemption_not_inversion():
    _, r = _analyse(EX / "overload.txt.gz")
    assert not _names(r, "possible_priority_inversion")
    (pre,) = _names(r, "cpu_preemption")
    assert pre["thread"].endswith("'A-control'") and pre["priority"] == [15]
    assert pre["ready_ms"] > 300
    assert pre["ran_instead"][0]["thread"].endswith("'B-worker'")


def test_fixed_overload_trace_has_no_pattern():
    """overload_fixed: the filtered fixture of the run with A at priority 30."""
    _, r = _analyse(FIX / "overload_fixed.txt.gz", threads=["A-control"])
    assert r["patterns"] == []
    assert r["threads"][0]["priorities"] == [30]


def test_the_tracer_itself_is_never_a_pattern():
    for f in EX.glob("*.txt.gz"):
        _, r = _analyse(f)
        assert not any(p["thread"].startswith("tracelogger[") for p in r["patterns"]), f.name


# -- per-thread report --------------------------------------------------------------------

def test_thread_report_waits_and_ready_latency():
    tr, r = _analyse(EX / "msgpass.txt.gz", threads=["A-control", "S-server"])
    a, srv = r["threads"]
    assert a["thread"].endswith("'A-control'") and a["state_ms"]["REPLY"] > 700
    w = a["longest_waits"][0]
    assert w["state"] in ("SEND", "REPLY") and "lower_priority_ran" in w
    assert w["lower_priority_ran"][0]["priority"] < 30
    s = srv["ready_waits"]
    assert s["count"] > 0 and s["p50_ms"] <= s["p99_ms"] <= s["max_ms"]
    rw = srv["longest_waits"][0]
    assert rw["state"] == "READY" and rw["ran_instead"][0]["thread"].endswith("'B-worker'")


def test_select_by_pid_tid_process_and_report_missing():
    tr = kev.parse(traceview.load_text(EX / "overload.txt.gz"))
    a = tr.find_thread("A-control")
    keys, missing = traceview.resolve(tr, [f"{a[0]}/{a[1]}", "nobody"], "rtdemo")
    assert keys[0] == a and missing == ["nobody"]
    assert all(k[0] == a[0] for k in keys) and len(keys) >= 3


def test_same_thread_names_in_one_process_get_their_tid():
    tr, r = _analyse(EX / "inversion.txt.gz", process="io-sock")
    labels = [t["thread"] for t in r["threads"]]
    assert len(labels) == len(set(labels))


def test_default_selection_skips_idle_shells():
    _, r = _analyse(EX / "inversion.txt.gz")
    assert not any(t["thread"].startswith("ksh[") for t in r["threads"])
    assert r["threads"][0]["thread"].endswith("'A-control'")


# -- input files --------------------------------------------------------------------------

def test_reads_plain_and_gzipped_text(tmp_path):
    plain = tmp_path / "t.txt"
    plain.write_bytes(gzip.decompress((EX / "overload.txt.gz").read_bytes()))
    assert traceview.load_text(plain) == traceview.load_text(EX / "overload.txt.gz")


def test_rejects_missing_and_non_trace_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        traceview.load_text(tmp_path / "nope.txt")
    other = tmp_path / "x.txt"
    other.write_text("hello\n")
    with pytest.raises(ValueError):
        kev.parse(traceview.load_text(other))


def test_kev_files_are_decoded_with_traceprinter(tmp_path, monkeypatch):
    kevf = tmp_path / "t.kev"
    kevf.write_bytes(b"\x00raw")
    text = traceview.load_text(EX / "overload.txt.gz")

    def fake_run(argv, **kw):
        assert argv[1:3] == ["-f", str(kevf)]
        Path(argv[4]).write_bytes(text.encode("utf-8"))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(tools, "_traceprinter", lambda: "traceprinter")
    monkeypatch.setattr("subprocess.run", fake_run)
    assert traceview.load_text(kevf) == text


# -- Perfetto export ---------------------------------------------------------------------------

def _perfetto(name):
    tr = kev.parse(traceview.load_text(EX / name))
    r = traceview.analyse(tr)
    return tr, traceview.perfetto(tr, r["_found"])


def test_perfetto_slices_never_overlap_on_a_track():
    """Perfetto moves overlapping complete events to a spill track and counts an import
    error for each; rounding the start and the length separately used to cause that."""
    for f in EX.glob("*.txt.gz"):
        _, d = _perfetto(f.name)
        tracks: dict = {}
        for e in d["traceEvents"]:
            if e["ph"] == "X":
                assert e["dur"] > 0 and e["ts"] >= 0
                tracks.setdefault((e["pid"], e["tid"]), []).append((e["ts"], e["ts"] + e["dur"]))
        for key, v in tracks.items():
            v.sort()
            assert all(b[0] >= a[1] - 1e-9 for a, b in zip(v, v[1:])), (f.name, key)


def test_perfetto_thread_tracks_match_the_trace():
    tr, d = _perfetto("msgpass.txt.gz")
    ev = d["traceEvents"]
    a = tr.find_thread("A-control")
    names = {(e["pid"], e.get("tid")): e["args"]["name"] for e in ev if e["ph"] == "M"
             and e["name"] in ("thread_name", "process_name")}
    assert names[(a[0], a[1])] == "A-control"
    assert names[(a[0], None)].startswith("rtdemo [")
    reply_ms = sum(e["dur"] for e in ev if e["ph"] == "X" and (e["pid"], e["tid"]) == a
                   and e["name"] == "REPLY") / 1000
    assert reply_ms == pytest.approx(tr.state_totals(a)["REPLY"], abs=0.01)
    assert all(e["args"]["priority"] == 30 for e in ev if e["ph"] == "X"
               and (e["pid"], e["tid"]) == a and e["name"] == "RUNNING")


def test_perfetto_cpu_tracks_hint_markers_and_order():
    tr, d = _perfetto("msgpass.txt.gz")
    ev = d["traceEvents"]
    cpu = [e for e in ev if e["ph"] == "X" and e["pid"] == traceview.CPU_PID]
    assert {e["tid"] for e in cpu} == {0, 1}
    assert any(e["name"].endswith("'B-worker'") for e in cpu)
    hints = [e for e in ev if e["ph"] == "i"]
    assert len(hints) == 1 and hints[0]["name"] == "hint: possible priority inversion"
    assert "candidate" in hints[0]["args"]["cannot_tell"]
    order = sorted((e["args"]["sort_index"], e["pid"]) for e in ev
                   if e["ph"] == "M" and e["name"] == "process_sort_index" and e["pid"])
    client = tr.find_thread("A-control")[0]
    server_pid = tr.find_thread("S-server")[0]
    assert [p for _, p in order[:2]] == [client, server_pid]       # the story first
    assert d["otherData"]["cpus"] == 2 and d["displayTimeUnit"] == "ms"


def test_site_samples_are_what_the_code_makes_today(tmp_path):
    """docs/trace-msgpass.html and docs/traces/msgpass.perfetto.json are on the project
    site; rebuild them from the recorded trace and check nothing has drifted. To update:
    qbridge trace --file examples/traces/msgpass.txt.gz --html docs/trace-msgpass.html
    --perfetto docs/traces/msgpass.perfetto.json"""
    s = tools.Session(NoTarget(), Policy())
    tools.trace_file(s, str(EX / "msgpass.txt.gz"), html_out=str(tmp_path / "p.html"),
                     perfetto_out=str(tmp_path / "p.json"))
    docs = ROOT / "docs"
    assert (tmp_path / "p.html").read_bytes() == (docs / "trace-msgpass.html").read_bytes()
    assert (tmp_path / "p.json").read_bytes() == (docs / "traces" / "msgpass.perfetto.json").read_bytes()


def test_cli_writes_the_perfetto_file(tmp_path, capsys):
    rc = cli.main(["trace", "--file", str(EX / "overload.txt.gz"),
                   "--html", str(tmp_path / "o.html"), "--perfetto", str(tmp_path / "o.json")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["perfetto"] == str(tmp_path / "o.json") and "ui.perfetto.dev" in out["perfetto_how"]
    d = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert any(e["ph"] == "i" and "CPU preemption" in e["name"] for e in d["traceEvents"])


# -- tool, CLI and MCP ----------------------------------------------------------------------

def test_tool_writes_an_html_page_with_the_timeline(tmp_path):
    s = tools.Session(NoTarget(), Policy())
    out = tools.trace_file(s, str(EX / "msgpass.txt.gz"), html_out=str(tmp_path / "p.html"))
    assert "_found" not in out and out["html"] == str(tmp_path / "p.html")
    page = (tmp_path / "p.html").read_text(encoding="utf-8")
    assert "Possible priority inversion" in page and "<svg" in page
    assert "S-server" in page and "waiting for a server (SEND/REPLY)" in page
    json.dumps(out)                                     # plain JSON, nothing left over


def test_cli_trace_file_needs_no_target_and_is_audited_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Target", type("T", (), {"from_env": staticmethod(lambda: NoTarget())}))
    rc = cli.main(["trace", "--file", str(EX / "overload.txt.gz"), "--thread", "A-control",
                   "--html", str(tmp_path / "o.html")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["threads"][0]["thread"].endswith("'A-control'")
    last = audit.read()[-1]
    assert last["tool"] == "qnx_trace_file" and last["changes_target"] is False


def test_cli_refuses_program_and_file_together(capsys):
    assert cli.main(["trace", "rtdemo", "--file", str(EX / "overload.txt.gz")]) == 1
    assert "not both" in capsys.readouterr().err


def test_mcp_tool_is_read_only_and_works_without_write_access(tmp_path):
    s = tools.Session(NoTarget(), Policy(allow_write=False))
    srv = server.Server(s)
    listed = {t["name"]: t for t in srv.tool_list()}
    assert listed["qnx_trace_file"]["annotations"]["readOnlyHint"] is True
    res = srv.call_tool("qnx_trace_file", {"path": str(EX / "inversion.txt.gz")})
    assert res["isError"] is False
    assert res["structuredContent"]["patterns"][0]["pattern"] == "possible_priority_inversion"
    bad = srv.call_tool("qnx_trace_file", {"path": str(tmp_path / "missing.txt")})
    assert bad["isError"] is True
