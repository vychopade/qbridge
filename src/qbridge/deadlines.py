"""Deadline statistics and miss attribution from per-cycle samples.

A sample is one release of a periodic task, with its timing split into parts
that have different causes (all in microseconds):

    backlog    the previous cycle ran past this release, so this one could
               not start on time (a knock-on effect, not a new cause)
    latency    scheduling latency: wake-up minus the moment the task could
               first have started
    lock_wait  time blocked waiting for a lock
    exec       CPU time the task itself used
    preempted  derived: response minus all of the above, i.e. time the task
               was ready to run but something else had the CPU
    response   finish minus release; compared with the deadline

Statistics are computed here from the raw samples, not copied from the
program's own summary, so they can be cross-checked.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass

PARTS = ("backlog", "latency", "lock_wait", "exec", "preempted")


@dataclass
class Sample:
    release: float
    response: float
    backlog: float = 0.0
    latency: float = 0.0
    lock_wait: float = 0.0
    exec: float = 0.0

    @property
    def preempted(self) -> float:
        rest = self.response - self.backlog - self.latency - self.lock_wait - self.exec
        return max(0.0, rest)


def parse_samples_csv(text: str) -> list[Sample]:
    """CSV with columns release_us, response_us and optionally backlog_us,
    latency_us, lock_wait_us, exec_us (the format rtdemo writes)."""
    rows = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    out = []
    for r in rows:
        def f(name: str) -> float:
            v = r.get(f"{name}_us")
            return float(v) if v not in (None, "") else 0.0
        if r.get("release_us") in (None, "") or r.get("response_us") in (None, ""):
            continue
        out.append(Sample(release=f("release"), response=f("response"), backlog=f("backlog"),
                          latency=f("latency"), lock_wait=f("lock_wait"), exec=f("exec")))
    return out


def stats(values: list[float]) -> dict:
    """max, mean, stddev, p50, p99 and min; empty input gives zeros.

    Percentiles use the nearest-rank-below rule rtdemo uses, so the two agree.
    """
    if not values:
        return {"count": 0, "min": 0.0, "max": 0.0, "mean": 0.0, "stddev": 0.0,
                "p50": 0.0, "p99": 0.0}
    v = sorted(values)
    n = len(v)
    mean = sum(v) / n
    var = sum((x - mean) ** 2 for x in v) / n
    return {
        "count": n,
        "min": v[0],
        "max": v[-1],
        "mean": mean,
        "stddev": math.sqrt(var),
        "p50": v[int((n - 1) * 0.50)],
        "p99": v[int((n - 1) * 0.99)],
    }


def _primary_cause(s: Sample, exec_budget: float | None) -> str:
    parts = {p: getattr(s, p) for p in PARTS}
    if exec_budget is not None:
        # Only the CPU time beyond the task's normal budget is a cause.
        parts["exec"] = max(0.0, s.exec - exec_budget)
    else:
        parts["exec"] = 0.0
    return max(parts, key=parts.get)


def analyse(samples: list[Sample], deadline_us: float, period_us: float | None = None,
            task: str = "task") -> dict:
    """Deadline report for one task."""
    n = len(samples)
    missed = [s for s in samples if s.response > deadline_us]
    # The task's normal CPU use per cycle: the median over all cycles.
    exec_budget = stats([s.exec for s in samples])["p50"] if samples else None

    # Group consecutive misses into chains; a chain's root is its first cycle.
    chains: list[list[Sample]] = []
    for i, s in enumerate(samples):
        if s.response <= deadline_us:
            continue
        if chains and i > 0 and samples[i - 1].response > deadline_us:
            chains[-1].append(s)
        else:
            chains.append([s])

    root_causes: dict[str, int] = {}
    chain_reports = []
    for ch in chains:
        root = ch[0]
        cause = _primary_cause(root, exec_budget)
        root_causes[cause] = root_causes.get(cause, 0) + 1
        chain_reports.append({
            "first_release_us": root.release,
            "misses": len(ch),
            "root_cause": cause,
            "root": {"response_us": root.response, **{f"{p}_us": getattr(root, p) for p in PARTS}},
            "worst_response_us": max(s.response for s in ch),
        })

    # Per-miss primary cause, before chain grouping (most knock-on misses are "backlog").
    per_miss: dict[str, int] = {}
    for s in missed:
        c = _primary_cause(s, exec_budget)
        per_miss[c] = per_miss.get(c, 0) + 1

    longest = max((len(c) for c in chains), default=0)
    worst_chains = sorted(chain_reports, key=lambda c: c["worst_response_us"], reverse=True)[:5]

    report = {
        "task": task,
        "period_us": period_us,
        "deadline_us": deadline_us,
        "cycles": n,
        "misses": len(missed),
        "miss_rate": (len(missed) / n) if n else 0.0,
        "miss_chains": len(chains),
        "longest_chain": longest,
        "response_us": stats([s.response for s in samples]),
        "parts_us": {p: stats([getattr(s, p) for s in samples]) for p in PARTS},
        "exec_budget_us": exec_budget,
        "chain_root_causes": root_causes,
        "per_miss_primary_cause": per_miss,
        "worst_chains": worst_chains,
    }
    report["summary"] = summarise(report)
    return report


_CAUSE_TEXT = {
    "lock_wait": "waiting for a lock",
    "latency": "late wake-up (scheduling latency)",
    "preempted": "being preempted while ready to run",
    "exec": "using more CPU time than usual",
    "backlog": "the previous cycle overrunning",
}


def summarise(r: dict) -> str:
    if r["cycles"] == 0:
        return f"{r['task']}: no cycles recorded."
    resp = r["response_us"]
    text = (f"{r['task']}: {r['misses']} of {r['cycles']} cycles missed the "
            f"{r['deadline_us'] / 1000:g} ms deadline ({r['miss_rate']:.1%}); worst response "
            f"{resp['max'] / 1000:.1f} ms, 99th percentile {resp['p99'] / 1000:.1f} ms, "
            f"mean {resp['mean'] / 1000:.2f} ms.")
    if r["misses"]:
        causes = sorted(r["chain_root_causes"].items(), key=lambda kv: -kv[1])
        parts = ", ".join(f"{_CAUSE_TEXT.get(c, c)} ({n})" for c, n in causes)
        text += (f" The misses form {r['miss_chains']} run(s) of consecutive misses "
                 f"(longest {r['longest_chain']}); what started each run: {parts}.")
    return text


def compare(before: dict, after: dict) -> dict:
    """Before/after comparison of two reports for the same task.

    Worst case and misses come first; the verdict is deliberately cautious.
    """
    def d(path: list[str]) -> dict:
        a = before
        b = after
        for k in path:
            a, b = a[k], b[k]
        return {"before": a, "after": b, "change": b - a}

    out = {
        "misses": d(["misses"]),
        "worst_response_us": d(["response_us", "max"]),
        "p99_response_us": d(["response_us", "p99"]),
        "mean_response_us": d(["response_us", "mean"]),
        "worst_lock_wait_us": d(["parts_us", "lock_wait", "max"]),
        "worst_latency_us": d(["parts_us", "latency", "max"]),
    }
    deadline = after["deadline_us"]
    if before["misses"] > 0 and after["misses"] == 0 and after["response_us"]["max"] < deadline:
        verdict = "improved"
        text = (f"No misses after the change (before: {before['misses']}), and the worst response "
                f"fell from {before['response_us']['max'] / 1000:.1f} ms to "
                f"{after['response_us']['max'] / 1000:.1f} ms. This holds for these runs only; it "
                "does not prove the deadline can never be missed.")
    elif after["misses"] < before["misses"] and after["response_us"]["max"] < before["response_us"]["max"]:
        verdict = "partly improved"
        text = (f"Fewer misses ({before['misses']} -> {after['misses']}) and a lower worst response, "
                "but the task still misses its deadline.")
    elif after["misses"] > before["misses"] and after["response_us"]["max"] >= before["response_us"]["max"]:
        verdict = "worse"
        text = f"More misses after the change ({before['misses']} -> {after['misses']})."
    else:
        verdict = "inconclusive"
        text = ("The measurements do not show a clear difference: misses and worst-case response "
                "moved in different directions or not at all.")
    out["verdict"] = verdict
    out["explanation"] = text
    return out


def cross_check(report: dict, summary: dict | None) -> dict:
    """Compare qbridge's statistics with the program's own summary line."""
    if not summary:
        return {"checked": False, "reason": "the program printed no JSON summary"}
    problems = []
    if summary.get("cycles") is not None and summary["cycles"] != report["cycles"]:
        problems.append(f"cycles: program {summary['cycles']}, samples {report['cycles']}")
    if summary.get("misses") is not None and summary["misses"] != report["misses"]:
        problems.append(f"misses: program {summary['misses']}, samples {report['misses']}")
    prog_max = (summary.get("response") or {}).get("max_us")
    if prog_max is not None and abs(prog_max - report["response_us"]["max"]) > 1.0:
        problems.append(f"worst response: program {prog_max} us, samples "
                        f"{report['response_us']['max']:.1f} us")
    return {"checked": True, "agrees": not problems, "problems": problems}
