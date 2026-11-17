"""Parse pidin thread listings into threads and a blocked-on graph.

qbridge runs pidin with a '|'-delimited format so names with spaces parse
cleanly (QNX-specific: pidin's -F format codes):

    pidin -F '%a|%b|%n|%p|%J|%B|%h'
    pid | tid | process path | priority+policy | state | blocked on | thread name

Meaning of the "Blocked" column depends on the thread state (checked on a
QNX 8.0 target):
  - REPLY / SEND: pid of the server process the thread is waiting on
  - RECEIVE: id of the channel the thread is receiving on (not a pid)
  - SEM / CONDVAR / MUTEX: address of the sync object (hex)
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

PIDIN_FORMAT = "%a|%b|%n|%p|%J|%B|%h"
PIDIN_ARGS = ["pidin", "-F", PIDIN_FORMAT]

# States where the Blocked column names another process.
_PID_STATES = {"SEND", "REPLY"}

_POLICY = {"f": "FIFO", "r": "RR", "o": "OTHER", "s": "SPORADIC", "i": "INTERRUPT"}
_PRIO_RE = re.compile(r"^(\d+)([a-z]?)$")
_BLOCKED_PID_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


@dataclass
class Thread:
    pid: int
    tid: int
    path: str
    name: str
    priority: int | None
    policy: str | None
    state: str
    blocked: str
    blocked_on_pid: int | None
    thread_name: str
    # Only available from qbridged (/dev/qbridge/threads), not from pidin.
    blocked_ms: int | None = None
    chid: int | None = None
    real_priority: int | None = None
    last_cpu: int | None = None

    def to_dict(self) -> dict:
        extras = ("blocked_ms", "chid", "real_priority", "last_cpu")
        return {k: v for k, v in asdict(self).items() if not (k in extras and v is None)}


def _split_prio(text: str) -> tuple[int | None, str | None]:
    m = _PRIO_RE.match(text)
    if not m:
        return None, None
    letter = m.group(2)
    return int(m.group(1)), _POLICY.get(letter, letter or None)


def parse_threads(text: str) -> list[Thread]:
    threads: list[Thread] = []
    for line in text.lstrip("﻿").splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 7 or not cols[0].isdigit():
            continue  # header or blank line
        pid_s, tid_s, path, prio_s, state, blocked = cols[:6]
        thread_name = "|".join(cols[6:]).strip()
        prio, policy = _split_prio(prio_s)
        blocked_on = None
        if state in _PID_STATES:
            m = _BLOCKED_PID_RE.match(blocked)
            if m:
                blocked_on = int(m.group(1))
        threads.append(Thread(
            pid=int(pid_s),
            tid=int(tid_s) if tid_s.isdigit() else 0,
            path=path,
            name=path.rsplit("/", 1)[-1],
            priority=prio,
            policy=policy,
            state=state,
            blocked=blocked,
            blocked_on_pid=blocked_on,
            thread_name=thread_name,
        ))
    return threads


def processes(threads: list[Thread]) -> list[dict]:
    """Group threads by process, keeping first-seen order."""
    procs: dict[int, dict] = {}
    for t in threads:
        p = procs.setdefault(t.pid, {"pid": t.pid, "name": t.name, "path": t.path, "threads": []})
        p["threads"].append({k: v for k, v in t.to_dict().items() if k not in ("pid", "name", "path")})
    return list(procs.values())


def blocked_graph(threads: list[Thread]) -> list[dict]:
    """Edges 'thread X is waiting on process Y', for SEND/REPLY-blocked threads."""
    names = {t.pid: t.name for t in threads}
    edges = []
    for t in threads:
        if t.blocked_on_pid is None:
            continue
        edge = {
            "from_pid": t.pid,
            "from_tid": t.tid,
            "from_name": t.name,
            "from_priority": t.priority,
            "state": t.state,
            "to_pid": t.blocked_on_pid,
            "to_name": names.get(t.blocked_on_pid),
        }
        if t.chid is not None:
            edge["chid"] = t.chid
        if t.blocked_ms is not None:
            edge["blocked_ms"] = t.blocked_ms
        edges.append(edge)
    return edges


def find_cycles(edges: list[dict]) -> list[list[int]]:
    """Process-level cycles in the blocked-on graph (a likely deadlock)."""
    adj: dict[int, set[int]] = {}
    for e in edges:
        adj.setdefault(e["from_pid"], set()).add(e["to_pid"])
    cycles: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()

    def walk(start: int, node: int, path: list[int]) -> None:
        for nxt in adj.get(node, ()):
            if nxt == start:
                cyc = path[:]
                k = cyc.index(min(cyc))
                key = tuple(cyc[k:] + cyc[:k])
                if key not in seen:
                    seen.add(key)
                    cycles.append(list(key))
            elif nxt not in path and nxt > start:
                walk(start, nxt, path + [nxt])

    for s in sorted(adj):
        walk(s, s, [s])
    return cycles
