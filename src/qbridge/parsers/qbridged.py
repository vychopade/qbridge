"""Read the JSON that qbridged serves under /dev/qbridge on the target.

qbridged is qbridge's QNX-native resource manager (target/qbridged). It
reads thread state straight from procfs (DCMD_PROC_TIDSTATUS), so it knows
things pidin's text output does not: how long each thread has been blocked,
the exact channel a SEND/REPLY thread is waiting on, and full thread names.
"""

from __future__ import annotations

import json

from .pidin import Thread

THREADS_PATH = "/dev/qbridge/threads"
CRASHES_PATH = "/dev/qbridge/crashes"
VERSION_PATH = "/dev/qbridge/version"


def parse_threads(text: str) -> list[Thread]:
    data = json.loads(text)
    out: list[Thread] = []
    for t in data["threads"]:
        b = t.get("blocked_on") or {}
        state = t["state"]
        to_pid = b.get("pid") if state in ("SEND", "REPLY") else None
        if "pid" in b and "chid" in b:
            blocked = f"{b['pid']} chid {b['chid']}"
        elif "chid" in b:
            blocked = f"chid {b['chid']}"
        elif "sync" in b:
            blocked = b["sync"]
        elif "tid" in b:
            blocked = f"tid {b['tid']}"
        else:
            blocked = ""
        out.append(Thread(
            pid=t["pid"],
            tid=t["tid"],
            path=t.get("path") or t["name"],
            name=t["name"],
            priority=t["priority"],
            policy=t["policy"],
            state=state,
            blocked=blocked,
            blocked_on_pid=to_pid,
            thread_name=t.get("thread_name", ""),
            blocked_ms=t.get("blocked_ms"),
            chid=b.get("chid"),
            real_priority=t.get("real_priority"),
            last_cpu=t.get("last_cpu"),
        ))
    return out


def parse_procs(text: str) -> dict[int, dict]:
    return {p["pid"]: p for p in json.loads(text)["procs"]}


DEADLINES_PATH = "/dev/qbridge/deadlines"
SAMPLES_PATH = "/dev/qbridge/samples"

DEADLINE_FIELDS = ("response", "backlog", "latency", "lock_wait", "exec")


def parse_deadlines(text: str) -> dict:
    """/dev/qbridge/deadlines: per-task statistics kept by qbridged.

    Tasks report each cycle to qbridged with MsgSendPulse (see
    target/qbridged/qbridge_task.h). Percentiles cover the most recent
    `window` cycles; max and mean cover every cycle since registration.
    """
    d = json.loads(text)
    for t in d.get("tasks", []):
        t["miss_rate"] = (t["misses"] / t["cycles"]) if t.get("cycles") else 0.0
    return d


def summarise_task(t: dict) -> str:
    r = t["response"]
    state = "running" if t["active"] else "finished"
    return (f"{t['name']} (pid {t['pid']}, {state}): {t['misses']} of {t['cycles']} cycles missed "
            f"the {t['deadline_us'] / 1000:g} ms deadline ({t['miss_rate']:.1%}); worst response "
            f"{r['max_us'] / 1000:.1f} ms, 99th percentile {r['p99_us'] / 1000:.1f} ms, worst lock "
            f"wait {t['lock_wait']['max_us'] / 1000:.1f} ms, worst scheduling latency "
            f"{t['latency']['max_us'] / 1000:.1f} ms.")


def cross_check(report: dict, task: dict, tol_us: float = 1.5) -> dict:
    """Compare qbridge's statistics from the workload's CSV with qbridged's."""
    problems = []
    if report["cycles"] != task["cycles"]:
        problems.append(f"cycles: samples {report['cycles']}, qbridged {task['cycles']}")
    if report["misses"] != task["misses"]:
        problems.append(f"misses: samples {report['misses']}, qbridged {task['misses']}")
    for f in DEADLINE_FIELDS:
        mine = report["response_us"] if f == "response" else report["parts_us"][f]
        if abs(mine["max"] - task[f]["max_us"]) > tol_us:
            problems.append(f"{f} max: samples {mine['max']:.1f} us, qbridged {task[f]['max_us']} us")
    return {"checked": True, "slot": task["slot"], "agrees": not problems, "problems": problems}


EVENTS_PATH = "/dev/qbridge/events"

# Short-lived helpers that qbridge's own ssh commands start. Their deaths are
# noise when you are watching your program, so they are hidden by default.
NOISE = {
    "sh", "ksh", "sleep", "cat", "timeout", "sshd-session", "sshd", "pidin", "slog2info",
    "stat", "chmod", "mv", "mkdir", "uname", "sftp-server", "scp", "grep", "ls", "dd",
    "tail", "head", "true", "wc",
}


def parse_events(text: str) -> list[dict]:
    """JSON lines from /dev/qbridge/events. A cut-off last line is ignored."""
    events = []
    for line in text.lstrip("﻿").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def filter_events(events: list[dict], since: int | None = None,
                  include_noise: bool = False) -> tuple[list[dict], int]:
    """Drop events at or before seq `since` and (by default) helper-process deaths.

    Returns (kept, number hidden as noise). Core events are always kept.
    """
    kept, hidden = [], 0
    for e in events:
        if since is not None and e.get("seq", 0) <= since:
            continue
        if not include_noise and e.get("type") == "death" and e.get("name") in NOISE:
            hidden += 1
            continue
        kept.append(e)
    return kept, hidden
