"""`qbridge replay`: check qbridge's analysis without a QNX target.

The repository keeps data recorded on the reference QNX 8.0 target (a 2-CPU
VirtualBox VM) under tests/fixtures: thread-state snapshots and deadline
reports from diagnose runs, kernel traces from the same kind of runs, and the
per-run reports of four fix experiments (one of them a fix that should not help). This command runs qbridge's real
analysis code on that data on this computer, and compares each result with
what qbridge reported on the target at the time.

It needs no target, no QNX SDP and no network. It shows that the analysis is
deterministic and reproduces the recorded results; it does not measure
anything new.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from . import analysis, deadlines, traceconfirm
from .parsers import kev

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

SCENARIOS = [
    ("scenario_inversion", "rtdemo, mutex without priority inheritance"),
    ("scenario_overload_fault", "rtdemo -s overload -a 15 (A below the CPU-heavy B)"),
    ("scenario_overload_fixed", "rtdemo -s overload -a 30 (the fix)"),
    ("scenario_msgpass_fault", "rtdemo -s msgpass, server channel with _NTO_CHF_FIXED_PRIORITY"),
    ("scenario_msgpass_fixed", "rtdemo -s msgpass, server inherits its client's priority (the fix)"),
]
TRACES = [
    ("inversion", "rtdemo, mutex without priority inheritance"),
    ("overload_fault", "rtdemo -s overload -a 15"),
    ("msgpass_fault", "rtdemo -s msgpass (fixed-priority server channel)"),
]
RULE_NAMES = {analysis.RULE_MUTEX_INVERSION: "Priority inversion on a mutex",
              analysis.RULE_CPU_PREEMPTION: "CPU preemption",
              analysis.RULE_MSGPASS_INVERSION: "Priority inversion through message passing"}
EXPERIMENTS = [
    ("experiment_live", "source change: PTHREAD_PRIO_NONE -> PTHREAD_PRIO_INHERIT"),
    ("experiment_live_overload", "scheduling change: A's priority 15 -> 30"),
    ("experiment_live_msgpass", "source change: drop _NTO_CHF_FIXED_PRIORITY from the server's channel"),
    ("experiment_live_wrongfix", "a fix that should not help: A's priority 30 -> 50, server channel "
                                 "still _NTO_CHF_FIXED_PRIORITY"),
]


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _date(stamp: str) -> str:
    s = stamp or ""
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 and s[:8].isdigit() else "?"


def _finding_key(f: dict) -> tuple:
    return (f["rule"], f["confidence"], f["title"])


def run() -> dict:
    if not FIXTURES.is_dir():
        raise FileNotFoundError(f"recorded data not found at {FIXTURES}: run this from a clone "
                                "of the repository (pip install -e .)")
    checks = []

    for name, what in SCENARIOS:
        d = _load(f"{name}.json")
        res = analysis.diagnose(d["snapshots"], d["deadline_report"], d["timing_checks"])
        rec = d["result"]
        # The recording was made with the rules of that day; compare those. Findings of
        # rules added since are shown, but they are new analysis, not a reproduction.
        old = [f for f in res["findings"] if f["rule"] in analysis.ORIGINAL_RULES]
        new = [f for f in res["findings"] if f["rule"] not in analysis.ORIGINAL_RULES]
        same = [_finding_key(f) for f in old] == [_finding_key(f) for f in rec["findings"]]
        checks.append({
            "kind": "diagnosis", "name": name, "what": what, "matches_target": same,
            "recorded": _date(rec.get("diagnosis_id", "")),
            "input": f"{len(d['snapshots'])} snapshots + the run's deadline report",
            "result": [f"{f['title']} ({f['confidence']} confidence)" for f in old]
                      or ["no finding"],
            "detail": old[0]["evidence"][:2] if old else [res["summary"]] if not new else [],
            "newer_rules": [f"{f['title']} ({f['confidence']} confidence)" for f in new]})

    for name, what in TRACES:
        meta = _load(f"traces/{name}.json")
        tr = kev.parse(gzip.decompress((FIXTURES / "traces" / f"{name}.txt.gz").read_bytes())
                       .decode("utf-8"))
        statuses, detail = [], []
        for f in meta["findings"]:
            c = traceconfirm.confirm(dict(f), tr, meta["trace_id"])
            statuses.append(c["status"])
            detail += [("seen: " if s["supported"] else "NOT shown: ") + s["detail"] for s in c["steps"]]
        checks.append({
            "kind": "trace", "name": name, "what": what,
            "matches_target": statuses == meta["live_status"],
            "recorded": _date(meta["trace_id"]),
            "input": f"kernel trace, {tr.duration_ms:.0f} ms, "
                     f"{sum(tr.event_counts.values())} events (filtered to rtdemo and CPU 1)",
            "result": [f"{RULE_NAMES.get(f['rule'], f['rule'])}: {s} by the kernel trace"
                       for f, s in zip(meta["findings"], statuses)],
            "detail": detail})

    for name, what in EXPERIMENTS:
        e = _load(f"{name}/experiment.json")

        def reports(side):
            return [_load(f"{name}/{r['run_id']}.report.json") for r in e[side]["runs"]]
        again = deadlines.compare_sets(reports("baseline"), reports("candidate"))
        m, w = again["misses"], again["worst_response_us"]
        checks.append({
            "kind": "experiment", "name": name, "what": what,
            "matches_target": again == e["comparison"],
            "recorded": _date(e["id"]),
            "input": f"{len(e['baseline']['runs'])} runs before, {len(e['candidate']['runs'])} after",
            "result": [f"verdict: {again['verdict']}"],
            "detail": [f"misses per run: {m['before']['runs']} -> {m['after']['runs']}",
                       f"worst response per run (ms): "
                       f"{[round(x / 1000, 1) for x in w['before']['runs']]} -> "
                       f"{[round(x / 1000, 1) for x in w['after']['runs']]}"]})

    return {"all_match": all(c["matches_target"] for c in checks), "checks": checks,
            "data": str(FIXTURES)}


def text(out: dict) -> str:
    lines = ["qbridge replay: qbridge's analysis run on data recorded on the QNX 8.0 target",
             "(2-CPU VirtualBox VM). No target is used; nothing new is measured.", ""]
    titles = {"diagnosis": "Diagnosis rules on recorded thread snapshots",
              "trace": "Findings checked against recorded kernel traces",
              "experiment": "Experiment verdicts from recorded runs"}
    for kind in ("diagnosis", "trace", "experiment"):
        lines.append(titles[kind])
        for c in (c for c in out["checks"] if c["kind"] == kind):
            mark = "same as on the target" if c["matches_target"] else "DIFFERS from the target"
            lines.append(f"  {c['what']}  [recorded {c['recorded']}; {c['input']}]")
            lines += [f"    -> {r}" for r in c["result"]]
            lines += [f"       {d}" for d in c["detail"]]
            lines.append(f"       ({mark})")
            lines += [f"    also, from a rule added after this was recorded: {r}"
                      for r in c.get("newer_rules", [])]
        lines.append("")
    n = len(out["checks"])
    ok = sum(c["matches_target"] for c in out["checks"])
    lines.append(f"{ok} of {n} results are the same as qbridge reported on the target.")
    lines.append("Full reports from those runs: docs/example-report.html, "
                 "docs/example-report-overload.html, docs/example-report-msgpass.html, "
                 "docs/example-report-wrongfix.html")
    return "\n".join(lines) + "\n"
