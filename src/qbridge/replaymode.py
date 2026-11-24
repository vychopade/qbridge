"""`qbridge serve --replay`: the MCP tools answered from data recorded on the target.

For anyone without a QNX target (a judge, a reviewer): connect an AI assistant to
this server and walk through the same loop as on the target, measure -> diagnose
-> confirm with a kernel trace -> propose -> a person approves -> measure the
change -> verdict, on the rtdemo workload.

What is real and what is not:
- Every answer comes from data recorded on the reference QNX 8.0 target
  (tests/fixtures, examples/traces). Nothing runs on a target and nothing is
  measured now; each result says when its data was recorded.
- The analysis is qbridge's real code run again on that data: the diagnosis
  rules, the kernel-trace checks, the trace analysis and the experiment verdicts.
- The approval is real: a proposed change still needs a person to type 'yes' to
  'qbridge experiment approve <id>' in a terminal. No source file is changed.
- Only the changes that were measured on the target can be "measured" here; any
  other proposal is refused with the list of recorded ones, never invented.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from . import analysis, deadlines, experiments, traceconfirm, traceview, tools
from .experiments import ExperimentError
from .parsers import kev
from .policy import PolicyError

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures"
EXAMPLES = ROOT / "examples"
SITE = "https://vychopade.github.io/qbridge/"
PROGRAM = "rtdemo"

# Workload arguments -> recorded data. "fixed" is used after a source fix was kept.
SCENARIOS = {
    "inversion": {"args": [], "snapshots": "scenario_inversion", "trace": "inversion",
                  "runs": ("experiment_live", "baseline"), "full_trace": "inversion"},
    "overload-15": {"args": ["-s", "overload", "-a", "15"], "snapshots": "scenario_overload_fault",
                    "trace": "overload_fault", "runs": ("experiment_live_overload", "baseline"),
                    "full_trace": "overload"},
    "overload-30": {"args": ["-s", "overload", "-a", "30"], "snapshots": "scenario_overload_fixed",
                    "trace": "overload_fixed", "runs": ("experiment_live_overload", "candidate")},
    "msgpass": {"args": ["-s", "msgpass"], "snapshots": "scenario_msgpass_fault",
                "trace": "msgpass_fault", "runs": ("experiment_live_msgpass", "baseline"),
                "full_trace": "msgpass"},
    "msgpass-fixed": {"args": ["-s", "msgpass"], "snapshots": "scenario_msgpass_fixed",
                      "trace": "msgpass_fixed", "runs": ("experiment_live_msgpass", "candidate"),
                      "full_trace": "msgpass_fixed"},
    "msgpass-50": {"args": ["-s", "msgpass", "-a", "50"],
                   "runs": ("experiment_live_wrongfix", "candidate")},
    "inversion-fixed": {"args": [], "runs": ("experiment_live", "candidate")},
}

# The changes that were measured on the target, by recording.
RECORDED_CHANGES = {
    "experiment_live": {
        "baseline_args": [], "kind": "source", "file": "rtdemo.c",
        "what": "rtdemo.c: LOCK_PROTOCOL PTHREAD_PRIO_NONE -> PTHREAD_PRIO_INHERIT",
        "report": "example-report.html", "fixed": "inversion-fixed"},
    "experiment_live_msgpass": {
        "baseline_args": ["-s", "msgpass"], "kind": "source", "file": "rtdemo.c",
        "what": "rtdemo.c: SERVER_CHANNEL_FLAGS _NTO_CHF_FIXED_PRIORITY -> 0",
        "report": "example-report-msgpass.html", "fixed": "msgpass-fixed"},
    "experiment_live_overload": {
        "baseline_args": ["-s", "overload", "-a", "15"], "kind": "args",
        "new_args": ["-s", "overload", "-a", "30"],
        "what": "arguments: -s overload -a 15 -> -s overload -a 30",
        "report": "example-report-overload.html"},
    "experiment_live_wrongfix": {
        "baseline_args": ["-s", "msgpass"], "kind": "args",
        "new_args": ["-s", "msgpass", "-a", "50"],
        "what": "arguments: -s msgpass -> -s msgpass -a 50 (should not help)",
        "report": "example-report-wrongfix.html"},
}


def _load(rel: str) -> Any:
    return json.loads((FIX / rel).read_text(encoding="utf-8"))


def _date(stamp: str) -> str:
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if stamp[:8].isdigit() else "?"


def _note(when: str) -> str:
    return (f"Replay: recorded on the QNX 8.0 target on {when}; nothing was run now. The "
            "analysis below is qbridge's code run again on that data.")


class ReplayState:
    """What the replayed session has done so far: which fixes were kept, and a
    counter so repeated measurements step through the recorded runs."""

    def __init__(self):
        self.kept: set[str] = set()
        self.measured = 0


def _scenario(args: list[str] | None, st: ReplayState) -> str:
    a = list(args or [])
    a = [x for i, x in enumerate(a) if not (x == "-s" and i + 1 < len(a) and a[i + 1] == "inversion")
         and not (i > 0 and a[i - 1] == "-s" and x == "inversion")]
    for key, sc in SCENARIOS.items():
        if sc["args"] == a and not key.endswith("-fixed"):
            fixed = {"inversion": "inversion-fixed", "msgpass": "msgpass-fixed"}.get(key)
            return fixed if fixed in st.kept else key
    raise ExperimentError(
        f"no recording for 'rtdemo {' '.join(a)}'. Replay mode has: rtdemo (mutex inversion), "
        "rtdemo -s overload -a 15 (CPU preemption), rtdemo -s overload -a 30, rtdemo -s msgpass "
        "(message-passing inversion) and rtdemo -s msgpass -a 50.")


def _check_program(program: str | None) -> None:
    if program and program != PROGRAM:
        raise ExperimentError(f"replay mode only has recordings of {PROGRAM}, not {program!r}")


def _run_reports(name: str, side: str) -> list[tuple[str, dict]]:
    e = _load(f"{name}/experiment.json")
    return [(r["run_id"], _load(f"{name}/{r['run_id']}.report.json")) for r in e[side]["runs"]]


# -- tools --------------------------------------------------------------------------------

def info(s) -> dict:
    return {"mode": "replay", "host": None, "write_allowed": False,
            "target": "recorded: QNX 8.0.0 x86_64, 2 CPUs (VirtualBox VM), Nov 17-23, 2026",
            "workloads": ["rtdemo", "rtdemo -s overload -a 15", "rtdemo -s overload -a 30",
                          "rtdemo -s msgpass", "rtdemo -s msgpass -a 50"],
            "recorded_changes": [c["what"] for c in RECORDED_CHANGES.values()],
            "note": ("Replay mode: no target is connected. Tools answer from data recorded on "
                     "the target, re-analysed by qbridge's code. A proposed change still needs "
                     "a person to run 'qbridge experiment approve <id>' in a terminal.")}


def timing_check(s, cpu: int = 1, seconds: int = 10) -> dict:
    tc = next(t for t in _load(f"{SCENARIOS['msgpass']['snapshots']}.json")["timing_checks"]
              if t["cpu"] == int(cpu))
    return {**tc, "replay": _note(tc.get("measured", "?")[:10])}


def measure(s, st: ReplayState, program: str, seconds: int = 10, args: list[str] | None = None,
            label: str | None = None, deadline_us: float | None = None) -> dict:
    _check_program(program)
    key = _scenario(args, st)
    name, side = SCENARIOS[key]["runs"]
    runs_ = _run_reports(name, side)
    run_id, rep = runs_[st.measured % len(runs_)]
    st.measured += 1
    return {"replay": _note(_date(run_id)), "run_id": run_id, "program": PROGRAM,
            "args": SCENARIOS[key]["args"], "seconds": 10, "summary": rep["summary"],
            "report": rep}


def diagnose(s, st: ReplayState, program: str | None = None, seconds: int = 6,
             snapshots: int = 25, run_id: str | None = None, args: list[str] | None = None,
             trace: bool = False, trace_seconds: float = 1.0) -> dict:
    _check_program(program)
    key = _scenario(args, st)
    sc = SCENARIOS[key]
    if "snapshots" not in sc:
        raise ExperimentError(f"no diagnosis was recorded for 'rtdemo {' '.join(sc['args'])}'"
                              f"{' with the fix kept' if key.endswith('-fixed') else ''}; "
                              "qnx_measure has its runs")
    d = _load(f"{sc['snapshots']}.json")
    res = analysis.diagnose(d["snapshots"], d["deadline_report"], d["timing_checks"])
    when = _date(d["result"].get("diagnosis_id", ""))
    res["replay"] = _note(when)
    res["diagnosis_id"] = d["result"].get("diagnosis_id")
    if trace:
        meta = _load(f"traces/{sc['trace']}.json")
        text = __import__("gzip").decompress((FIX / "traces" / f"{sc['trace']}.txt.gz")
                                             .read_bytes()).decode("utf-8")
        tr = kev.parse(text)
        by_rule = {f["rule"]: f for f in meta["findings"]}
        for f in res["findings"]:
            same_run = by_rule.get(f["rule"])
            if same_run is None:
                f["trace"] = {"status": "not checked",
                              "reason": "no finding of this rule in the recorded traced run"
                              if f["rule"] in analysis.ORIGINAL_RULES
                              else f"no trace check for rule {f['rule']!r}"}
                continue
            # The trace comes from another run of the same scenario (thread ids differ),
            # so its own run's finding is the one checked, step by step, again now.
            c = traceconfirm.confirm(dict(same_run), tr, meta["trace_id"])
            c["note"] = (f"Trace recorded {_date(meta['trace_id'])} in another run of the same "
                         "workload; the check uses that run's finding, re-checked now.")
            f["trace"] = c
            f["confirmed_by_trace"] = c["status"] == "confirmed"
        n_ok = sum(1 for f in res["findings"] if f.get("confirmed_by_trace"))
        n = sum(1 for f in res["findings"] if (f.get("trace") or {}).get("status") != "not checked")
        res["summary"] += (f" Kernel trace ({tr.duration_ms:.0f} ms, recorded): {n_ok} of {n} "
                           "finding(s) confirmed step by step." if n else "")
    return res


def trace(s, st: ReplayState, program: str | None = None, args: list[str] | None = None,
          seconds: int = 6, trace_seconds: float = 1.0, threads: list[str] | None = None) -> dict:
    _check_program(program)
    key = _scenario(args, st)
    full = SCENARIOS[key].get("full_trace")
    if not full:
        raise ExperimentError(f"no kernel trace was recorded for 'rtdemo {' '.join(SCENARIOS[key]['args'])}'")
    path = EXAMPLES / "traces" / f"{full}.txt.gz"
    out = tools.trace_file(s, str(path), threads, PROGRAM if not threads else None)
    out["replay"] = _note("2026-11-22") + f" Full trace: examples/traces/{full}.txt.gz."
    return out


# -- experiments ------------------------------------------------------------------------------

def exp_start(s, st: ReplayState, program: str, project_dir: str | None = None,
              binary: str | None = None, seconds: int = 10, repeats: int = 3,
              args: list[str] | None = None) -> dict:
    _check_program(program)
    key = _scenario(args, st)
    if key not in ("inversion", "overload-15", "msgpass"):
        raise ExperimentError("replay experiments start from a recorded fault: rtdemo, "
                              "rtdemo -s overload -a 15 or rtdemo -s msgpass")
    name, side = SCENARIOS[key]["runs"]
    rec = _load(f"{name}/experiment.json")
    base = f"replay-{time.strftime('%Y%m%d-%H%M%S')}-{PROGRAM}"
    exp_id, n = base, 2
    while (experiments.exp_dir() / exp_id).exists():
        exp_id, n = f"{base}-{n}", n + 1
    e = {"id": exp_id, "replay": True, "state": "baseline",
         "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "program": PROGRAM,
         "project_dir": str(EXAMPLES / PROGRAM), "binary": f"build/{PROGRAM}",
         "workload": {"seconds": 10, "repeats": 3, "args": SCENARIOS[key]["args"]},
         "baseline": rec["baseline"], "recording": name,
         "history": [{"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "event": f"baseline replayed from {name} (recorded {_date(rec['id'])})"}]}
    (experiments.exp_dir() / exp_id).mkdir(parents=True, exist_ok=True)
    experiments._save(e)
    return {"experiment_id": exp_id, "state": "baseline", "replay": _note(_date(rec["id"])),
            "baseline": [{"run_id": r["run_id"], "summary": rep["summary"]}
                         for r, (_, rep) in zip(rec["baseline"]["runs"], _run_reports(name, "baseline"))],
            "next": "Propose ONE change with qnx_experiment_propose; a person approves it."}


def _source_fixes(new: str) -> set[str]:
    """Which recorded source fixes the edited rtdemo.c makes, however the edit is phrased."""
    import re
    out = set()
    if re.search(r"^#define\s+LOCK_PROTOCOL\s+PTHREAD_PRIO_INHERIT\b", new, re.M):
        out.add("experiment_live")
    m = re.search(r"^#define\s+SERVER_CHANNEL_FLAGS\b(.*)$", new, re.M)
    create = re.search(r"ChannelCreate\(([^)]*)\)", new)
    if m and create and "_NTO_CHF_FIXED_PRIORITY" not in m.group(1) \
            and "_NTO_CHF_FIXED_PRIORITY" not in create.group(1):
        out.add("experiment_live_msgpass")
    return out


def _matching_recording(e: dict) -> str | None:
    p = e["proposal"]
    for name, c in RECORDED_CHANGES.items():
        if c["baseline_args"] != e["workload"]["args"] or c["kind"] != p["kind"]:
            continue
        if p["kind"] == "args" and p["new_args"] == c["new_args"]:
            return name
        if p["kind"] == "source" and p["file"] == c["file"]:
            src = (Path(e["project_dir"]) / p["file"]).read_text(encoding="utf-8")
            if src.count(p["find"]) == 1 and name in _source_fixes(src.replace(p["find"], p["replace"], 1)):
                return name
    return None


def exp_propose(s, st: ReplayState, experiment_id: str, file: str | None = None,
                find: str | None = None, replace: str | None = None, description: str = "",
                diagnosis_id: str | None = None, args: list[str] | None = None) -> dict:
    e = experiments.load(experiment_id)
    if not e.get("replay"):
        raise ExperimentError("not a replay experiment")
    out = experiments.propose(s, experiment_id, file, find, replace, description, diagnosis_id, args)
    e = experiments.load(experiment_id)
    rec = _matching_recording(e)
    out["replay"] = ("This change was measured on the target; once approved, qnx_experiment_run "
                     f"replays that measurement ({rec})." if rec else
                     "Note: this change was NOT measured on the target, so replay mode cannot "
                     "measure it. Recorded changes: " + "; ".join(
                         c["what"] for c in RECORDED_CHANGES.values()
                         if c["baseline_args"] == e["workload"]["args"]))
    return out


def exp_run(s, st: ReplayState, experiment_id: str) -> dict:
    e = experiments.load(experiment_id)
    if not e.get("replay"):
        raise ExperimentError("not a replay experiment")
    p, a = e.get("proposal"), e.get("approval")
    if e["state"] != "approved" or not p or not a:
        raise PolicyError("this change has not been approved by a person; run "
                          f"'qbridge experiment approve {experiment_id}' in a terminal")
    if a["diff_sha256"] != p["diff_sha256"]:
        raise PolicyError("the approval was for a different diff; approve again")
    name = _matching_recording(e)
    if not name:
        raise ExperimentError("this change was not measured on the target, so replay mode has no "
                              "result for it; propose one of: " + "; ".join(
                                  c["what"] for c in RECORDED_CHANGES.values()
                                  if c["baseline_args"] == e["workload"]["args"]))
    rec = _load(f"{name}/experiment.json")
    before = [rep for _, rep in _run_reports(name, "baseline")]
    after = [rep for _, rep in _run_reports(name, "candidate")]
    cmp = deadlines.compare_sets(before, after)          # the real verdict code, again
    note = _note(_date(rec["id"]))
    if rec["baseline"]["runs"] != e["baseline"]["runs"]:
        note += (" The change was measured in its own session, so it is compared with that "
                 "session's baseline runs (virtual-machine numbers vary between boots).")
        e["baseline"] = rec["baseline"]
    e["candidate"] = rec["candidate"]
    e["comparison"] = cmp
    e["recording"] = name
    e["state"] = "measured"
    e["history"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "event": f"candidate replayed from {name}: {cmp['verdict']}"})
    experiments._save(e)
    return {"experiment_id": experiment_id, "state": "measured", "verdict": cmp["verdict"],
            "explanation": cmp["explanation"], "comparison": cmp, "replay": note,
            "next": "Keep the change (qnx_experiment_keep) or roll back (qnx_experiment_rollback); "
                    "qnx_report gives the HTML report recorded for this experiment."}


def _finish(experiment_id: str, state: str, event: str, st: ReplayState) -> dict:
    e = experiments.load(experiment_id)
    if not e.get("replay"):
        raise ExperimentError("not a replay experiment")
    if e["state"] != "measured":
        raise ExperimentError(f"experiment is {e['state']}; run it first")
    fixed = RECORDED_CHANGES.get(e.get("recording"), {}).get("fixed")
    if state == "kept" and fixed:
        st.kept.add(fixed)
    elif fixed:
        st.kept.discard(fixed)
    e["state"] = state
    e["history"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event})
    experiments._save(e)
    return {"experiment_id": experiment_id, "state": state,
            "replay": "Nothing was changed on a target or in your files."
                      + (" Later measurements and diagnoses of this workload use the recording "
                         "of the fixed build." if state == "kept" and fixed else "")}


def exp_rollback(s, st: ReplayState, experiment_id: str) -> dict:
    return _finish(experiment_id, "rolled_back", "rolled back (replay)", st)


def exp_keep(s, st: ReplayState, experiment_id: str) -> dict:
    return _finish(experiment_id, "kept", "kept (replay)", st)


def report(s, st: ReplayState, experiment_id: str, diagnosis_id: str | None = None,
           out: str | None = None) -> dict:
    e = experiments.load(experiment_id)
    name = e.get("recording")
    if not e.get("replay") or name not in RECORDED_CHANGES or "comparison" not in e:
        raise ExperimentError("run the replay experiment first; the report is the one recorded "
                              "for that measurement")
    page = RECORDED_CHANGES[name]["report"]
    return {"report": str(ROOT / "docs" / page), "url": SITE + page,
            "replay": "This is the HTML report qbridge wrote on the target for the recorded "
                      "experiment, with its charts and kernel-trace timeline."}


# -- the tool table ----------------------------------------------------------------------------

def tool_table(st: ReplayState) -> dict[str, tuple[str, dict, Callable[..., dict], bool]]:
    """The MCP tools in replay mode: the same names and inputs as on a target, read-only."""
    from .server import TOOLS
    handlers: dict[str, Callable[..., dict]] = {
        "qnx_info": info,
        "qnx_timing_check": timing_check,
        "qnx_measure": lambda s, **k: measure(s, st, **k),
        "qnx_diagnose": lambda s, **k: diagnose(s, st, **k),
        "qnx_trace": lambda s, **k: trace(s, st, **k),
        "qnx_trace_file": TOOLS["qnx_trace_file"][2],
        "qnx_experiment_start": lambda s, **k: exp_start(s, st, **k),
        "qnx_experiment_propose": lambda s, **k: exp_propose(s, st, **k),
        "qnx_experiment_run": lambda s, **k: exp_run(s, st, **k),
        "qnx_experiment_rollback": lambda s, **k: exp_rollback(s, st, **k),
        "qnx_experiment_keep": lambda s, **k: exp_keep(s, st, **k),
        "qnx_experiment_show": TOOLS["qnx_experiment_show"][2],
        "qnx_report": lambda s, **k: report(s, st, **k),
        "qnx_audit": TOOLS["qnx_audit"][2],
    }
    out = {}
    for name, h in handlers.items():
        desc, schema = TOOLS[name][0], TOOLS[name][1]
        out[name] = ("[Replay mode: answers from data recorded on the QNX target; nothing runs "
                     "now.] " + desc, schema, h, False)
    return out


INSTRUCTIONS = (
    "qbridge RT in REPLAY MODE: no QNX target is connected. The tools answer from data recorded "
    "on a QNX 8.0 target (a 2-CPU VirtualBox VM, Nov 17-23, 2026) and run qbridge's real analysis "
    "on it again; every result says when its data was recorded. Say so when you report results. "
    "The workload is rtdemo, a 10 ms control task 'A-control' (priority 30) with a CPU-heavy "
    "worker (20) and a low-priority logger or server (10). Recorded workloads: 'rtdemo' (no "
    "args), '-s overload -a 15', '-s overload -a 30', '-s msgpass', '-s msgpass -a 50'. Typical "
    "order, as on a target: qnx_timing_check, qnx_measure, qnx_diagnose (trace=true to check "
    "findings against a recorded kernel trace), qnx_experiment_start, qnx_experiment_propose "
    "(one exact change; the source is examples/rtdemo/rtdemo.c), then ask the user to approve it "
    "by running 'qbridge experiment approve <id>' in a terminal (you cannot approve it), "
    "qnx_experiment_run, qnx_report, and qnx_experiment_rollback or _keep. Only changes that "
    "were measured on the target can be run; qnx_info lists them. Report results as measured: "
    "finite runs do not prove a worst case, and say 'inconclusive' when the tools do."
)
