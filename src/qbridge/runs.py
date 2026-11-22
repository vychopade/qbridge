"""Saved measurement runs on the host.

Every qnx_measure call stores what it captured, so later steps (comparison,
experiments, the HTML report) are built only from recorded data:

    <home>/runs/<run_id>/meta.json      what ran: program, args, binary sha256, time
    <home>/runs/<run_id>/summary.json   the program's own JSON summary line
    <home>/runs/<run_id>/samples.csv    per-cycle samples, as fetched from the target
    <home>/runs/<run_id>/report.json    statistics computed by qbridge from samples.csv

<home> is QBRIDGE_HOME, or ~/.qbridge.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


def home() -> Path:
    if os.environ.get("QBRIDGE_HOME"):
        return Path(os.environ["QBRIDGE_HOME"])
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".qbridge"


def runs_dir() -> Path:
    return home() / "runs"


def new_run_id(label: str | None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", label or "run").strip("-")[:40] or "run"
    run_id = f"{stamp}-{safe}"
    n = 2
    while (runs_dir() / run_id).exists():
        run_id = f"{stamp}-{safe}-{n}"
        n += 1
    return run_id


def save(run_id: str, meta: dict, summary: dict | None, samples_csv: str, report: dict) -> Path:
    d = runs_dir() / run_id
    d.mkdir(parents=True, exist_ok=False)
    (d / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (d / "samples.csv").write_text(samples_csv, encoding="utf-8", newline="\n")
    (d / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return d


def load(run_id: str) -> dict:
    d = runs_dir() / run_id
    if not d.is_dir() or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        raise FileNotFoundError(f"no saved run {run_id!r}")
    out = {"run_id": run_id,
           "meta": json.loads((d / "meta.json").read_text(encoding="utf-8")),
           "report": json.loads((d / "report.json").read_text(encoding="utf-8"))}
    if (d / "summary.json").exists():
        out["summary"] = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    return out


def save_diagnosis(program: str | None, result: dict, snapshots: list[list[dict]]) -> str:
    """Save a diagnosis with the snapshots that supported its findings.

    To keep files small, only snapshots in which a thread of the named
    processes was MUTEX-blocked or READY (waiting for the CPU) are kept, with
    those processes' threads plus every RUNNING thread (who had the CPU).
    With no findings, the processes that had a MUTEX-blocked thread are used.
    """
    pids = {f["subject"]["pid"] for f in result.get("findings", [])}
    for f in result.get("findings", []):
        pids |= {m["pid"] for m in f["subject"].get("middle", [])}
    if not pids:
        pids = {t["pid"] for snap in snapshots for t in snap if t.get("state") == "MUTEX"}
    waiting = ("MUTEX", "READY")
    kept = [[t for t in snap if t["pid"] in pids or t.get("state") == "RUNNING"]
            for snap in snapshots
            if any(t["pid"] in pids and t.get("state") in waiting for t in snap)]
    diag_id = new_run_id(f"diagnose-{program or 'live'}")
    d = home() / "diagnoses"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{diag_id}.json").write_text(
        json.dumps({"diagnosis_id": diag_id, "program": program,
                    "taken": time.strftime("%Y-%m-%dT%H:%M:%S"), "result": result,
                    "snapshots_kept": kept}, indent=1) + "\n", encoding="utf-8")
    return diag_id


def save_timing_check(result: dict) -> str:
    check_id = new_run_id(f"timing-cpu{result.get('cpu', 'x')}")
    d = home() / "timing"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{check_id}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return check_id


def _stamp_seconds(stamp: str) -> float | None:
    """'YYYYMMDD-HHMMSS...' (the start of every qbridge id) -> epoch seconds."""
    try:
        return time.mktime(time.strptime(stamp[:15], "%Y%m%d-%H%M%S"))
    except ValueError:
        return None


def latest_timing_checks(near: str | None = None) -> list[dict]:
    """The saved timing check per CPU: the most recent one, or with `near` (an
    id such as an experiment id) the one taken closest in time to it, so a
    report on old measurements quotes the timer noise of that day."""
    d = home() / "timing"
    if not d.is_dir():
        return []
    target = _stamp_seconds(near) if near else None
    best: dict = {}
    for f in sorted(d.glob("*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        cpu = r.get("cpu")
        if target is not None:
            t = _stamp_seconds(f.stem)
            dist = abs(t - target) if t is not None else float("inf")
            if cpu in best and best[cpu][0] <= dist:
                continue
            best[cpu] = (dist, {**r, "check_id": f.stem})
        else:
            best[cpu] = (0, {**r, "check_id": f.stem})
    return [best[k][1] for k in sorted(best, key=lambda c: (c is None, c))]


def load_diagnosis(diag_id: str) -> dict:
    if "/" in diag_id or "\\" in diag_id or diag_id.startswith("."):
        raise FileNotFoundError(f"bad diagnosis id {diag_id!r}")
    f = home() / "diagnoses" / f"{diag_id}.json"
    if not f.is_file():
        raise FileNotFoundError(f"no saved diagnosis {diag_id!r}")
    return json.loads(f.read_text(encoding="utf-8"))


def latest_diagnosis(program: str) -> dict | None:
    d = home() / "diagnoses"
    if not d.is_dir():
        return None
    for f in sorted(d.glob(f"*-diagnose-{program}.json"), reverse=True):
        return json.loads(f.read_text(encoding="utf-8"))
    return None


def samples_text(run_id: str) -> str | None:
    f = runs_dir() / run_id / "samples.csv"
    return f.read_text(encoding="utf-8") if f.is_file() else None


def list_runs(limit: int = 50) -> list[dict]:
    if not runs_dir().is_dir():
        return []
    items = []
    for d in sorted(runs_dir().iterdir(), reverse=True):
        if not (d / "meta.json").exists():
            continue
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        rep = json.loads((d / "report.json").read_text(encoding="utf-8"))
        items.append({"run_id": d.name, "label": meta.get("label"), "program": meta.get("program"),
                      "started": meta.get("started"), "cycles": rep.get("cycles"),
                      "misses": rep.get("misses"),
                      "worst_response_us": rep.get("response_us", {}).get("max")})
        if len(items) >= limit:
            break
    return items
