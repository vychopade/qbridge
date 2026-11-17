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
