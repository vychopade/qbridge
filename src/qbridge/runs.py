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

    To keep files small, only snapshots in which some thread was MUTEX-blocked
    are kept, and only the processes named in the findings (or, with no
    findings, the processes that had a MUTEX-blocked thread).
    """
    pids = {f["subject"]["pid"] for f in result.get("findings", [])}
    for f in result.get("findings", []):
        pids |= {m["pid"] for m in f["subject"].get("middle", [])}
    if not pids:
        pids = {t["pid"] for snap in snapshots for t in snap if t.get("state") == "MUTEX"}
    kept = [[t for t in snap if t["pid"] in pids] for snap in snapshots
            if any(t.get("state") == "MUTEX" for t in snap)]
    diag_id = new_run_id(f"diagnose-{program or 'live'}")
    d = home() / "diagnoses"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{diag_id}.json").write_text(
        json.dumps({"diagnosis_id": diag_id, "program": program,
                    "taken": time.strftime("%Y-%m-%dT%H:%M:%S"), "result": result,
                    "snapshots_kept": kept}, indent=1) + "\n", encoding="utf-8")
    return diag_id


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
