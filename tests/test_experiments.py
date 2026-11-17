"""Experiment loop with a fake build and measurements replayed from real rtdemo
runs (baseline = PTHREAD_PRIO_NONE samples, candidate = PTHREAD_PRIO_INHERIT)."""

import io
import json

import pytest

from qbridge import build as build_mod
from qbridge import cli, deadlines, experiments, runs, server, tools
from qbridge.policy import Policy, PolicyError
from qbridge.target import Target

SRC = '#define LOCK_PROTOCOL PTHREAD_PRIO_NONE\nint main(void) { return 0; }\n'
FIND = "#define LOCK_PROTOCOL PTHREAD_PRIO_NONE"
REPLACE = "#define LOCK_PROTOCOL PTHREAD_PRIO_INHERIT"


class TTY(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def env(tmp_path, monkeypatch, fixture_text):
    monkeypatch.setenv("QBRIDGE_HOME", str(tmp_path / "home"))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "rtdemo.c").write_text(SRC, encoding="utf-8")
    (project / "Makefile").write_text("all:\n", encoding="utf-8")
    state = {"deployed": [], "builds": 0, "fail_build": False}

    class FakeBuild:
        def __init__(self, ok, errors=()):
            self.ok, self.errors, self.warnings = ok, list(errors), []

        def to_dict(self):
            return {"ok": self.ok, "errors": self.errors, "warnings": []}

    def fake_build(path, **kw):
        state["builds"] += 1
        if state["fail_build"]:
            return FakeBuild(False, [{"file": "rtdemo.c", "line": 1, "message": "boom"}])
        src = (project / "rtdemo.c").read_text(encoding="utf-8")
        (project / "build").mkdir(exist_ok=True)
        (project / "build" / "rtdemo").write_bytes(b"ELF:" + src.encode())   # differs per source
        return FakeBuild(True)

    def fake_deploy(s, local, name=None):
        state["deployed"].append(open(local, "rb").read())
        return {"remote": f"/data/qbridge/{name}"}

    def fake_measure(s, program, seconds=10, args=None, label=None, deadline_us=None, period_us=None):
        variant = "rtdemo_fixed" if b"INHERIT" in state["deployed"][-1] else "rtdemo"
        samples = deadlines.parse_samples_csv(fixture_text(f"{variant}_samples.csv"))
        rep = deadlines.analyse(samples, 10000.0, 10000.0, "A-control")
        run_id = runs.new_run_id(label)
        runs.save(run_id, {"label": label}, None, fixture_text(f"{variant}_samples.csv"), rep)
        return {"run_id": run_id, "binary_sha256": "x", "summary": rep["summary"],
                "cross_check": {"checked": True, "agrees": True},
                "qbridged_check": {"checked": False}}

    import hashlib
    monkeypatch.setattr(build_mod, "build", fake_build)
    monkeypatch.setattr(experiments.build_mod, "build", fake_build)
    monkeypatch.setattr(experiments.tools, "deploy", fake_deploy)
    monkeypatch.setattr(experiments.tools, "measure", fake_measure)
    monkeypatch.setattr(experiments.tools, "_remote_sha256",
                        lambda s, p: hashlib.sha256(state["deployed"][-1]).hexdigest())
    s = tools.Session(Target(host="192.0.2.10"), Policy(allow_write=True))
    return s, project, state


def _start(s, project):
    return experiments.start(s, "rtdemo", str(project), seconds=10, repeats=2)


def test_full_loop_improves_and_rolls_back(env):
    s, project, state = env
    st = _start(s, project)
    eid = st["experiment_id"]
    assert st["state"] == "baseline" and len(st["baseline"]) == 2

    prop = experiments.propose(s, eid, "rtdemo.c", FIND, REPLACE, "restore priority inheritance")
    assert "-#define LOCK_PROTOCOL PTHREAD_PRIO_NONE" in prop["diff"]
    assert "+#define LOCK_PROTOCOL PTHREAD_PRIO_INHERIT" in prop["diff"]
    assert "qbridge experiment approve" in prop["approval"]
    assert (project / "rtdemo.c").read_text(encoding="utf-8") == SRC     # not applied yet

    with pytest.raises(PolicyError, match="not been approved"):
        experiments.run(s, eid)

    out = io.StringIO()
    assert cli.approve_interactively(eid, TTY("yes\n"), out)["approved"] is True
    assert "+#define LOCK_PROTOCOL PTHREAD_PRIO_INHERIT" in out.getvalue()

    res = experiments.run(s, eid)
    assert res["verdict"] == "improved"
    assert "No deadline was missed after the change" in res["explanation"]
    assert "does not prove a worst case" in res["explanation"]
    assert REPLACE in (project / "rtdemo.c").read_text(encoding="utf-8")
    e = experiments.show(eid)
    assert e["approval"]["via"] == "qbridge CLI prompt"
    assert e["comparison"]["misses"]["after"]["max"] == 0

    rb = experiments.rollback(s, eid)
    assert rb["target_binary_matches_baseline"] is True and rb["restored_files"] == ["rtdemo.c"]
    assert (project / "rtdemo.c").read_text(encoding="utf-8") == SRC
    assert state["deployed"][-1] == b"ELF:" + SRC.encode()
    events = [h["event"] for h in experiments.show(eid)["history"]]
    assert events[0] == "baseline measured" and events[-1] == "rolled back"


def test_approval_needs_an_interactive_person(env):
    s, project, _ = env
    eid = _start(s, project)["experiment_id"]
    experiments.propose(s, eid, "rtdemo.c", FIND, REPLACE, "fix")
    with pytest.raises(PolicyError, match="interactive terminal"):
        cli.approve_interactively(eid, io.StringIO("yes\n"), io.StringIO())
    assert cli.approve_interactively(eid, TTY("no\n"), io.StringIO())["approved"] is False
    assert experiments.show(eid)["state"] == "proposed"


def test_new_proposal_needs_new_approval(env):
    s, project, _ = env
    eid = _start(s, project)["experiment_id"]
    experiments.propose(s, eid, "rtdemo.c", FIND, REPLACE, "fix")
    cli.approve_interactively(eid, TTY("yes\n"), io.StringIO())
    experiments.propose(s, eid, "rtdemo.c", FIND, "#define LOCK_PROTOCOL PTHREAD_PRIO_PROTECT", "other")
    with pytest.raises(PolicyError, match="not been approved"):
        experiments.run(s, eid)


def test_proposal_rules(env):
    s, project, _ = env
    eid = _start(s, project)["experiment_id"]
    with pytest.raises(experiments.ExperimentError, match="occurs 3 times"):
        experiments.propose(s, eid, "rtdemo.c", "in", "X", "ambiguous")      # define, int, main
    with pytest.raises(experiments.ExperimentError, match="occurs 0 times"):
        experiments.propose(s, eid, "rtdemo.c", "not there", "X", "absent")
    with pytest.raises(PolicyError, match="outside the project"):
        experiments.propose(s, eid, "../other.c", FIND, REPLACE, "escape")
    with pytest.raises(experiments.ExperimentError, match="does not exist"):
        experiments.propose(s, eid, "missing.c", FIND, REPLACE, "missing")


def test_failed_build_reverts_the_change(env):
    s, project, state = env
    eid = _start(s, project)["experiment_id"]
    experiments.propose(s, eid, "rtdemo.c", FIND, REPLACE, "fix")
    cli.approve_interactively(eid, TTY("yes\n"), io.StringIO())
    state["fail_build"] = True
    with pytest.raises(experiments.ExperimentError, match="build failed"):
        experiments.run(s, eid)
    assert (project / "rtdemo.c").read_text(encoding="utf-8") == SRC
    assert experiments.show(eid)["history"][-1]["event"] == "build failed; change reverted"


def test_write_access_required(env):
    s, project, _ = env
    ro = tools.Session(s.target, Policy())
    with pytest.raises(PolicyError):
        experiments.start(ro, "rtdemo", str(project))


def test_no_approve_tool_in_mcp_server():
    names = set(server.TOOLS)
    assert {"qnx_experiment_start", "qnx_experiment_propose", "qnx_experiment_run",
            "qnx_experiment_rollback"} <= names
    assert not any("approve" in n for n in names)


def test_live_experiment_record(fixture_text):
    """The experiment run on the target on 2026-11-17, approved by a person at
    the CLI prompt, then rolled back."""
    e = json.loads(fixture_text("experiment_live/experiment.json"))
    assert [h["event"] for h in e["history"]] == [
        "baseline measured", "change proposed", "approved (qbridge CLI prompt)",
        "change applied", "candidate measured: improved", "rolled back"]
    assert e["approval"]["diff_sha256"] == e["proposal"]["diff_sha256"]
    assert "+#define LOCK_PROTOCOL PTHREAD_PRIO_INHERIT" in e["proposal"]["diff"]
    assert e["rollback"]["matches_baseline"] is True
    assert e["baseline"]["binary_sha256"] != e["candidate"]["binary_sha256"]
    assert all(r["cross_check"] and r["qbridged_check"]
               for r in e["baseline"]["runs"] + e["candidate"]["runs"])

    def reports(side):
        return [json.loads(fixture_text(f"experiment_live/{r['run_id']}.report.json"))
                for r in e[side]["runs"]]

    again = deadlines.compare_sets(reports("baseline"), reports("candidate"))
    assert again == e["comparison"]
    assert again["verdict"] == "improved"
    assert again["misses"]["before"]["runs"] == [310, 304, 302]
    assert again["misses"]["after"]["runs"] == [0, 0, 0]


def _rep(misses, worst):
    return {"misses": misses, "deadline_us": 10000.0,
            "response_us": {"max": worst, "p99": worst}, "parts_us": {"lock_wait": {"max": 0}}}


def test_compare_sets_is_strict():
    before = [_rep(300, 37000), _rep(290, 36000)]
    assert deadlines.compare_sets(before, [_rep(0, 4000), _rep(1, 10500)])["verdict"] == "improved"
    overlap = deadlines.compare_sets(before, [_rep(0, 4000), _rep(295, 38000)])
    assert overlap["verdict"] == "inconclusive" and "do not show a clear difference" in overlap["explanation"]
    assert deadlines.compare_sets(before, [_rep(400, 50000)])["verdict"] == "worse"
    same = deadlines.compare_sets([_rep(0, 4000)], [_rep(0, 4100)])
    assert same["verdict"] == "inconclusive" and "no misses either way" in same["explanation"]
