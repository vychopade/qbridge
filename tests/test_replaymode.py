"""`qbridge serve --replay`: the assistant loop on recorded data, with no target."""

import io
import json
from pathlib import Path

import pytest

from qbridge import cli, replaymode, server, tools
from qbridge.policy import Policy
from qbridge.target import Target

ROOT = Path(__file__).resolve().parents[1]
RTDEMO = ROOT / "examples" / "rtdemo" / "rtdemo.c"


class TTY(io.StringIO):
    def isatty(self):
        return True


class NoTarget(Target):
    def run(self, argv, timeout=60):
        raise AssertionError(f"replay mode ran a target command: {argv}")

    def get(self, *a, **k):
        raise AssertionError("replay mode fetched a file from a target")

    def put(self, *a, **k):
        raise AssertionError("replay mode copied a file to a target")


@pytest.fixture
def srv():
    s = tools.Session(NoTarget(host=None), Policy(allow_write=False))
    return server.Server(s, replaymode.tool_table(replaymode.ReplayState()), replaymode.INSTRUCTIONS)


def call(srv, name, **args):
    r = srv.call_tool(name, args)
    if r["isError"]:
        raise RuntimeError(r["content"][0]["text"])
    return r["structuredContent"]


def approve(eid, answer="yes\n"):
    return cli.approve_interactively(eid, TTY(answer), io.StringIO())


def test_only_the_loop_is_offered_and_every_tool_is_read_only(srv):
    listed = {t["name"]: t for t in srv.tool_list()}
    assert {"qnx_measure", "qnx_diagnose", "qnx_experiment_run", "qnx_report"} <= set(listed)
    assert not {"qnx_deploy", "qnx_run", "qnx_kill", "qnx_build", "qnx_ps"} & set(listed)
    assert all(t["annotations"]["readOnlyHint"] for t in listed.values())
    assert all(t["description"].startswith("[Replay mode") for t in listed.values())
    assert "REPLAY MODE" in replaymode.INSTRUCTIONS and "cannot approve" in replaymode.INSTRUCTIONS


def test_full_message_passing_loop_with_a_person_approving(srv):
    before = RTDEMO.read_bytes()
    m = call(srv, "qnx_measure", program="rtdemo", args=["-s", "msgpass"])
    assert m["report"]["misses"] > 800 and "nothing was run now" in m["replay"]

    d = call(srv, "qnx_diagnose", program="rtdemo", args=["-s", "msgpass"], trace=True)
    (f,) = d["findings"]
    assert f["rule"] == "msgpass_priority_inversion" and f["confirmed_by_trace"] is True
    assert "1 of 1 finding(s) confirmed" in d["summary"]

    eid = call(srv, "qnx_experiment_start", program="rtdemo", project_dir="x",
               args=["-s", "msgpass"])["experiment_id"]
    assert eid.startswith("replay-")
    p = call(srv, "qnx_experiment_propose", experiment_id=eid, file="rtdemo.c",
             find="#define SERVER_CHANNEL_FLAGS _NTO_CHF_FIXED_PRIORITY",
             replace="#define SERVER_CHANNEL_FLAGS 0", description="server inherits")
    assert "was measured on the target" in p["replay"]
    assert "qbridge experiment approve" in p["approval"]

    with pytest.raises(RuntimeError, match="not been approved by a person"):
        call(srv, "qnx_experiment_run", experiment_id=eid)
    with pytest.raises(Exception, match="interactive terminal"):
        cli.approve_interactively(eid, io.StringIO("yes\n"), io.StringIO())
    assert approve(eid)["approved"] is True

    r = call(srv, "qnx_experiment_run", experiment_id=eid)
    assert r["verdict"] == "improved"
    assert r["comparison"]["misses"]["after"]["runs"] == [9, 4, 24]
    assert call(srv, "qnx_report", experiment_id=eid)["url"].endswith("example-report-msgpass.html")

    call(srv, "qnx_experiment_keep", experiment_id=eid)
    after = call(srv, "qnx_diagnose", program="rtdemo", args=["-s", "msgpass"])
    assert after["findings"] == [] and "msgpass_inheritance_observed" in after
    assert RTDEMO.read_bytes() == before                     # the source was never edited


def test_the_wrong_fix_is_measured_as_not_helping(srv):
    eid = call(srv, "qnx_experiment_start", program="rtdemo", project_dir="x",
               args=["-s", "msgpass"])["experiment_id"]
    call(srv, "qnx_experiment_propose", experiment_id=eid, args=["-s", "msgpass", "-a", "50"],
         description="raise the client")
    approve(eid)
    r = call(srv, "qnx_experiment_run", experiment_id=eid)
    assert r["verdict"] == "inconclusive"
    assert "its own session" in r["replay"]           # compared with that session's baseline
    call(srv, "qnx_experiment_rollback", experiment_id=eid)


def test_scheduling_fix_and_the_same_fix_phrased_differently(srv):
    eid = call(srv, "qnx_experiment_start", program="rtdemo", project_dir="x",
               args=["-s", "overload", "-a", "15"])["experiment_id"]
    call(srv, "qnx_experiment_propose", experiment_id=eid, args=["-s", "overload", "-a", "30"],
         description="A above B")
    approve(eid)
    assert call(srv, "qnx_experiment_run", experiment_id=eid)["verdict"] == "improved"

    eid = call(srv, "qnx_experiment_start", program="rtdemo", project_dir="x")["experiment_id"]
    call(srv, "qnx_experiment_propose", experiment_id=eid, file="rtdemo.c",
         find="#define LOCK_PROTOCOL PTHREAD_PRIO_NONE",
         replace="#define LOCK_PROTOCOL  PTHREAD_PRIO_INHERIT   /* fixed */",
         description="inherit")
    approve(eid)
    assert call(srv, "qnx_experiment_run", experiment_id=eid)["verdict"] == "improved"


def test_an_unrecorded_change_is_refused_not_invented(srv):
    eid = call(srv, "qnx_experiment_start", program="rtdemo", project_dir="x",
               args=["-s", "overload", "-a", "15"])["experiment_id"]
    p = call(srv, "qnx_experiment_propose", experiment_id=eid, args=["-s", "overload", "-a", "25"],
             description="a bit higher")
    assert "NOT measured on the target" in p["replay"] and "-a 30" in p["replay"]
    approve(eid)
    with pytest.raises(RuntimeError, match="not measured on the target"):
        call(srv, "qnx_experiment_run", experiment_id=eid)
    with pytest.raises(RuntimeError, match="no recording for 'rtdemo -s overload -a 25'"):
        call(srv, "qnx_measure", program="rtdemo", args=["-s", "overload", "-a", "25"])
    with pytest.raises(RuntimeError, match="only has recordings of rtdemo"):
        call(srv, "qnx_measure", program="myapp")


def test_measure_steps_through_the_recorded_runs_and_trace_uses_the_full_trace(srv):
    a = call(srv, "qnx_measure", program="rtdemo", args=["-s", "overload", "-a", "15"])
    b = call(srv, "qnx_measure", program="rtdemo", args=["-s", "overload", "-a", "15"])
    assert a["run_id"] != b["run_id"]
    t = call(srv, "qnx_trace", program="rtdemo", args=["-s", "overload", "-a", "15"])
    assert any(p["pattern"] == "cpu_preemption" for p in t["patterns"])
    assert "examples/traces/overload.txt.gz" in t["replay"]
    info = call(srv, "qnx_info")
    assert info["mode"] == "replay" and len(info["recorded_changes"]) == 4


def test_replayed_calls_are_audited(srv):
    from qbridge import audit
    call(srv, "qnx_info")
    last = audit.read()[-1]
    assert last["tool"] == "qnx_info" and last["changes_target"] is False


def test_serve_replay_over_stdio(monkeypatch):
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "qnx_diagnose", "arguments": {"program": "rtdemo"}}}]
    out = io.StringIO()
    monkeypatch.setattr(server, "Target", lambda **k: NoTarget(**k))
    server.serve(stdin=io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), stdout=out,
                 replay=True)
    init, diag = [json.loads(l) for l in out.getvalue().splitlines()]
    assert "REPLAY MODE" in init["result"]["instructions"]
    res = diag["result"]["structuredContent"]
    assert res["findings"][0]["rule"] == "mutex_priority_inversion" and "Replay" in res["replay"]


def test_cli_accepts_serve_replay():
    a = cli.build_parser().parse_args(["serve", "--replay"])
    assert a.replay is True
