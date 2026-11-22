"""Measured fix experiments: baseline, proposal, human approval, test, compare.

    start     build the current source (baseline), deploy, measure N times,
              keep copies of the baseline binary and source files
    propose   ONE change, shown as a diff and not applied: either an exact
              text replacement in one source file, or new workload arguments
              (a scheduling parameter such as a thread priority, no source
              edit); -d and -o stay fixed so the workload is the same
    approve   ONLY from the qbridge CLI, by a person typing "yes" at a prompt
              (see cli.py). The MCP server has no approve tool, so an AI
              assistant cannot approve its own change. The approval records
              the diff's sha256; a different proposal needs a new approval.
    run       check approval; for a source edit apply, rebuild and redeploy;
              for new arguments redeploy the baseline binary (sha256 checked);
              rerun the workload N times, compare
    rollback  restore the source files and redeploy the saved baseline binary;
              checks the target's binary sha256 matches the baseline
    keep      leave the change applied

Everything is stored under <home>/experiments/<id>/ so the report can be
built from captured data only.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import shlex
import shutil
import time
from pathlib import Path

from . import build as build_mod
from . import deadlines, runs, tools
from .policy import PolicyError
from .target import TargetError

STATES = ("baseline", "proposed", "approved", "measured", "kept", "rolled_back")


class ExperimentError(RuntimeError):
    pass


def exp_dir() -> Path:
    return runs.home() / "experiments"


def _path(exp_id: str) -> Path:
    if not exp_id or "/" in exp_id or "\\" in exp_id or exp_id.startswith("."):
        raise ExperimentError(f"bad experiment id {exp_id!r}")
    d = exp_dir() / exp_id
    if not d.is_dir():
        raise ExperimentError(f"no experiment {exp_id!r}")
    return d


def load(exp_id: str) -> dict:
    return json.loads((_path(exp_id) / "experiment.json").read_text(encoding="utf-8"))


def _save(e: dict) -> None:
    d = exp_dir() / e["id"]
    d.mkdir(parents=True, exist_ok=True)
    e["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (d / "experiment.json").write_text(json.dumps(e, indent=2) + "\n", encoding="utf-8")


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _inside(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise PolicyError(f"{rel!r} is outside the project folder {root}")
    return p


def _source_files(project: Path) -> list[Path]:
    exts = {".c", ".h", ".cc", ".cpp", ".hpp"}
    files = [p for p in project.iterdir() if p.is_file() and (p.suffix in exts or p.name == "Makefile")]
    return sorted(files)


def _build_and_check(project: Path, binary_rel: str) -> tuple[Path, dict]:
    res = build_mod.build(project)
    if not res.ok:
        raise ExperimentError(f"build failed: {len(res.errors)} error(s): "
                              + "; ".join(f"{e.get('file')}:{e.get('line')} {e['message']}"
                                          for e in res.errors[:5]))
    binary = _inside(project, binary_rel)
    if not binary.is_file():
        raise ExperimentError(f"build succeeded but {binary} does not exist; pass binary=")
    return binary, res.to_dict()


def _measure_n(s, program: str, seconds: int, repeats: int, args: list[str], label: str) -> list[dict]:
    out = []
    for i in range(repeats):
        m = tools.measure(s, program, seconds, args, f"{label}-{i + 1}")
        out.append({"run_id": m["run_id"], "binary_sha256": m["binary_sha256"],
                    "summary": m["summary"], "cross_check": m["cross_check"]["agrees"]
                    if m["cross_check"].get("checked") else None,
                    "qbridged_check": m["qbridged_check"].get("agrees")})
    return out


def _reports(run_list: list[dict]) -> list[dict]:
    return [runs.load(r["run_id"])["report"] for r in run_list]


def start(s, program: str, project_dir: str, binary: str | None = None, seconds: int = 10,
          repeats: int = 3, args: list[str] | None = None) -> dict:
    s.policy.require_write("experiment start (builds, deploys and runs)")
    project = Path(project_dir).resolve()
    if not project.is_dir():
        raise ExperimentError(f"{project} is not a folder")
    repeats = max(1, min(int(repeats), 10))
    binary_rel = binary or f"build/{program}"

    exp_id = runs.new_run_id(f"exp-{program}")
    d = exp_dir() / exp_id
    (d / "baseline_src").mkdir(parents=True)
    for f in _source_files(project):
        shutil.copy2(f, d / "baseline_src" / f.name)

    bin_path, build_info = _build_and_check(project, binary_rel)
    shutil.copy2(bin_path, d / "baseline.bin")
    tools.deploy(s, str(bin_path), program)
    base_runs = _measure_n(s, program, seconds, repeats, args or [], f"{exp_id}-baseline")

    e = {
        "id": exp_id, "state": "baseline", "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "program": program, "project_dir": str(project), "binary": binary_rel,
        "workload": {"seconds": seconds, "repeats": repeats, "args": args or []},
        "baseline": {"binary_sha256": _sha256(d / "baseline.bin"),
                     "source_files": [f.name for f in _source_files(project)],
                     "runs": base_runs, "build_warnings": len(build_info["warnings"])},
        "history": [{"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "baseline measured"}],
    }
    _save(e)
    reps = _reports(base_runs)
    return {"experiment_id": exp_id, "state": e["state"], "baseline": base_runs,
            "baseline_misses": [r["misses"] for r in reps],
            "next": "Propose one change with qnx_experiment_propose."}


def _args_line(program: str, args: list[str]) -> str:
    return " ".join([program, *(shlex.quote(a) for a in args)]) + "\n"


# measure sets these itself; a proposal may not change them, or the before and
# after runs would no longer be the same workload.
_RESERVED_ARGS = ("-d", "-o")


def _propose_args(e: dict, args: list[str], description: str, diagnosis_id: str | None) -> dict:
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args) or len(args) > 32:
        raise ExperimentError("args must be a list of at most 32 strings")
    for a in args:
        if any(a == r or a.startswith(r) and len(a) > 2 for r in _RESERVED_ARGS) or "\x00" in a:
            raise ExperimentError(f"{a!r} cannot be changed by an experiment (qbridge sets -d and -o)")
    old = e["workload"]["args"]
    if args == old:
        raise ExperimentError("the proposed arguments are the same as the current ones")
    label = "workload arguments"
    diff = "".join(difflib.unified_diff([_args_line(e["program"], old)], [_args_line(e["program"], args)],
                                        fromfile=f"a/{label}", tofile=f"b/{label}"))
    return {"kind": "args", "old_args": old, "new_args": args, "description": description,
            "diagnosis_id": diagnosis_id, "diff": diff,
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            "proposed": time.strftime("%Y-%m-%dT%H:%M:%S")}


def propose(s, exp_id: str, file: str | None = None, find: str | None = None,
            replace: str | None = None, description: str = "",
            diagnosis_id: str | None = None, args: list[str] | None = None) -> dict:
    """Propose ONE change: either a source edit (file, find, replace) or new
    workload arguments (args), e.g. a different thread priority. Not applied."""
    e = load(exp_id)
    if e["state"] not in ("baseline", "proposed", "approved"):
        raise ExperimentError(f"experiment is {e['state']}; start a new one to try another change")
    if not description:
        raise ExperimentError("describe the change and why (description)")
    if args is not None:
        if file or find or replace:
            raise ExperimentError("propose either a source edit or new arguments, not both")
        e["proposal"] = _propose_args(e, args, description, diagnosis_id)
        return _record_proposal(e, exp_id)
    if not (file and find is not None and replace is not None):
        raise ExperimentError("a source edit needs file, find and replace (or pass args instead)")
    project = Path(e["project_dir"])
    target = _inside(project, file)
    if not target.is_file():
        raise ExperimentError(f"{file} does not exist in {project}")
    text = target.read_text(encoding="utf-8")
    count = text.count(find)
    if count != 1:
        raise ExperimentError(f"'find' text must occur exactly once in {file}; it occurs {count} times")
    new = text.replace(find, replace, 1)
    diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"a/{file}", tofile=f"b/{file}"))
    e["proposal"] = {"kind": "source", "file": file, "find": find, "replace": replace,
                     "description": description,
                     "diagnosis_id": diagnosis_id, "diff": diff,
                     "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                     "proposed": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return _record_proposal(e, exp_id)


def _record_proposal(e: dict, exp_id: str) -> dict:
    p = e["proposal"]
    e.pop("approval", None)                # any earlier approval was for a different diff
    e["state"] = "proposed"
    e["history"].append({"time": p["proposed"],
                         "event": "change proposed" + (" (arguments)" if p["kind"] == "args" else "")})
    _save(e)
    return {"experiment_id": exp_id, "state": "proposed", "kind": p["kind"],
            "description": p["description"], "diff": p["diff"],
            "approval": f"A person must approve this change before it is applied. In a terminal run:\n"
                        f"  qbridge experiment approve {exp_id}\n"
                        "It shows the diff and asks for 'yes'. The assistant cannot approve it."}


def approve(exp_id: str, answer: str, approver: str = "cli prompt") -> dict:
    """Called only from the CLI after a person typed the answer."""
    e = load(exp_id)
    if e["state"] != "proposed" or "proposal" not in e:
        raise ExperimentError(f"nothing to approve (experiment is {e['state']})")
    if answer.strip().lower() != "yes":
        e["history"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "approval declined"})
        _save(e)
        return {"approved": False}
    e["approval"] = {"diff_sha256": e["proposal"]["diff_sha256"], "via": approver,
                     "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    e["state"] = "approved"
    e["history"].append({"time": e["approval"]["time"], "event": f"approved ({approver})"})
    _save(e)
    return {"approved": True, "experiment_id": exp_id, "diff_sha256": e["approval"]["diff_sha256"]}


def run(s, exp_id: str) -> dict:
    s.policy.require_write("experiment run (changes source, builds, deploys, runs)")
    e = load(exp_id)
    p = e.get("proposal")
    a = e.get("approval")
    if e["state"] != "approved" or not p or not a:
        raise PolicyError("this change has not been approved by a person; run "
                          f"'qbridge experiment approve {exp_id}' in a terminal")
    if a["diff_sha256"] != p["diff_sha256"]:
        raise PolicyError("the approval was for a different diff; approve again")
    if p.get("kind") == "args":
        return _run_args(s, e, exp_id)
    project = Path(e["project_dir"])
    target = _inside(project, p["file"])
    text = target.read_text(encoding="utf-8")
    if text.count(p["find"]) != 1:
        raise ExperimentError(f"{p['file']} changed since the proposal; propose again")
    target.write_text(text.replace(p["find"], p["replace"], 1), encoding="utf-8")
    e["history"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "change applied"})
    _save(e)
    try:
        bin_path, build_info = _build_and_check(project, e["binary"])
    except ExperimentError:
        target.write_text(text, encoding="utf-8")   # leave the source as it was
        e["history"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "event": "build failed; change reverted"})
        _save(e)
        raise
    tools.deploy(s, str(bin_path), e["program"])
    w = e["workload"]
    cand_runs = _measure_n(s, e["program"], w["seconds"], w["repeats"], w["args"], f"{exp_id}-candidate")
    e["candidate"] = {"binary_sha256": _sha256(bin_path), "runs": cand_runs, "args": w["args"],
                      "build_warnings": len(build_info["warnings"])}
    return _finish_run(e, exp_id, cand_runs)


def _run_args(s, e: dict, exp_id: str) -> dict:
    """Approved argument change: same binary, new arguments, same workload length."""
    p = e["proposal"]
    if e["workload"]["args"] != p["old_args"]:
        raise ExperimentError("the workload arguments changed since the proposal; propose again")
    # Same binary as the baseline, checked by sha256, so only the arguments differ.
    baseline_bin = _path(exp_id) / "baseline.bin"
    tools.deploy(s, str(baseline_bin), e["program"])
    on_target = tools._remote_sha256(s, s.policy.deploy_path(e["program"]))
    if on_target != e["baseline"]["binary_sha256"]:
        raise TargetError(f"the target's binary is {on_target}, not the baseline "
                          f"{e['baseline']['binary_sha256']}; not comparable")
    e["history"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "event": "arguments applied: " + " ".join(p["new_args"])})
    w = e["workload"]
    cand_runs = _measure_n(s, e["program"], w["seconds"], w["repeats"], p["new_args"],
                           f"{exp_id}-candidate")
    e["candidate"] = {"binary_sha256": on_target, "runs": cand_runs, "args": p["new_args"],
                      "build_warnings": 0}
    return _finish_run(e, exp_id, cand_runs)


def _finish_run(e: dict, exp_id: str, cand_runs: list[dict]) -> dict:
    cmp = deadlines.compare_sets(_reports(e["baseline"]["runs"]), _reports(cand_runs))
    e["comparison"] = cmp
    e["state"] = "measured"
    e["history"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "event": f"candidate measured: {cmp['verdict']}"})
    _save(e)
    return {"experiment_id": exp_id, "state": "measured", "verdict": cmp["verdict"],
            "explanation": cmp["explanation"], "comparison": cmp,
            "next": "Keep the change (qnx_experiment_keep) or roll back to the baseline "
                    "(qnx_experiment_rollback)."}


def rollback(s, exp_id: str) -> dict:
    s.policy.require_write("experiment rollback (restores source, redeploys baseline)")
    e = load(exp_id)
    d = _path(exp_id)
    project = Path(e["project_dir"])
    restored = []
    for name in e["baseline"]["source_files"]:
        src = d / "baseline_src" / name
        dst = _inside(project, name)
        if src.is_file() and (not dst.is_file() or dst.read_bytes() != src.read_bytes()):
            shutil.copy2(src, dst)
            restored.append(name)
    tools.deploy(s, str(d / "baseline.bin"), e["program"])
    remote = s.policy.deploy_path(e["program"])
    on_target = tools._remote_sha256(s, remote)
    ok = on_target == e["baseline"]["binary_sha256"]
    e["state"] = "rolled_back"
    e["rollback"] = {"restored_files": restored, "target_sha256": on_target, "matches_baseline": ok,
                     "workload_args": e["workload"]["args"],
                     "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    e["history"].append({"time": e["rollback"]["time"],
                         "event": "rolled back" + ("" if ok else " (target sha256 does NOT match)")})
    _save(e)
    if not ok:
        raise TargetError(f"after rollback the target's {remote} has sha256 {on_target}, "
                          f"not the baseline's {e['baseline']['binary_sha256']}")
    return {"experiment_id": exp_id, "state": "rolled_back", "restored_files": restored,
            "target_binary_matches_baseline": ok,
            "workload_args": e["workload"]["args"]}


def keep(s, exp_id: str) -> dict:
    e = load(exp_id)
    if e["state"] != "measured":
        raise ExperimentError(f"only a measured experiment can be kept (it is {e['state']})")
    e["state"] = "kept"
    e["history"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "change kept"})
    out = {"experiment_id": exp_id, "state": "kept"}
    if e["proposal"].get("kind") == "args":
        # Nothing on the target stores arguments; record which ones to use.
        e["workload"]["kept_args"] = e["proposal"]["new_args"]
        out["use_arguments"] = e["proposal"]["new_args"]
        out["note"] = "Run the program with these arguments from now on (e.g. in its start script)."
    _save(e)
    return out


def show(exp_id: str) -> dict:
    return load(exp_id)


def list_all() -> list[dict]:
    if not exp_dir().is_dir():
        return []
    out = []
    for d in sorted(exp_dir().iterdir(), reverse=True):
        f = d / "experiment.json"
        if f.is_file():
            e = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": e["id"], "program": e["program"], "state": e["state"],
                        "change": (e.get("proposal") or {}).get("description"),
                        "verdict": (e.get("comparison") or {}).get("verdict")})
    return out
