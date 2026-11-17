"""Measured fix experiments: baseline, proposal, human approval, test, compare.

    start     build the current source (baseline), deploy, measure N times,
              keep copies of the baseline binary and source files
    propose   one exact text replacement in one source file; shown as a diff,
              not applied
    approve   ONLY from the qbridge CLI, by a person typing "yes" at a prompt
              (see cli.py). The MCP server has no approve tool, so an AI
              assistant cannot approve its own change. The approval records
              the diff's sha256; a different proposal needs a new approval.
    run       check approval, apply, rebuild, redeploy under the same name,
              rerun the identical workload N times, compare
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


def propose(s, exp_id: str, file: str, find: str, replace: str, description: str,
            diagnosis_id: str | None = None) -> dict:
    e = load(exp_id)
    if e["state"] not in ("baseline", "proposed", "approved"):
        raise ExperimentError(f"experiment is {e['state']}; start a new one to try another change")
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
    e["proposal"] = {"file": file, "find": find, "replace": replace, "description": description,
                     "diagnosis_id": diagnosis_id, "diff": diff,
                     "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                     "proposed": time.strftime("%Y-%m-%dT%H:%M:%S")}
    e.pop("approval", None)                # any earlier approval was for a different diff
    e["state"] = "proposed"
    e["history"].append({"time": e["proposal"]["proposed"], "event": "change proposed"})
    _save(e)
    return {"experiment_id": exp_id, "state": "proposed", "description": description, "diff": diff,
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
    return {"approved": True, "experiment_id": exp_id}


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
    cmp = deadlines.compare_sets(_reports(e["baseline"]["runs"]), _reports(cand_runs))
    e["candidate"] = {"binary_sha256": _sha256(bin_path), "runs": cand_runs,
                      "build_warnings": len(build_info["warnings"])}
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
                     "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    e["history"].append({"time": e["rollback"]["time"],
                         "event": "rolled back" + ("" if ok else " (target sha256 does NOT match)")})
    _save(e)
    if not ok:
        raise TargetError(f"after rollback the target's {remote} has sha256 {on_target}, "
                          f"not the baseline's {e['baseline']['binary_sha256']}")
    return {"experiment_id": exp_id, "state": "rolled_back", "restored_files": restored,
            "target_binary_matches_baseline": ok}


def keep(s, exp_id: str) -> dict:
    e = load(exp_id)
    if e["state"] != "measured":
        raise ExperimentError(f"only a measured experiment can be kept (it is {e['state']})")
    e["state"] = "kept"
    e["history"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "change kept"})
    _save(e)
    return {"experiment_id": exp_id, "state": "kept"}


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
