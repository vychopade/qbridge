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


def _label(t: Thread) -> str:
    who = f"{t.name}[{t.pid}]"
    return f"{who} thread '{t.thread_name}'" if t.thread_name and not t.thread_name.isdigit() else who


def find_inversions(threads: list[Thread]) -> list[dict]:
    """Threads waiting on a server that runs below their priority.

    On QNX a server thread normally inherits the priority of the client it is
    serving. When it does not (e.g. a channel made with _NTO_CHF_FIXED_PRIORITY),
    a high-priority client can wait behind any thread whose priority falls
    between the two. That is reported as active when such a thread is READY or
    RUNNING right now.
    """
    by_pid: dict[int, list[Thread]] = {}
    for t in threads:
        by_pid.setdefault(t.pid, []).append(t)
    found = []
    for c in threads:
        if c.state not in _PID_STATES or c.blocked_on_pid is None or c.priority is None:
            continue
        server = by_pid.get(c.blocked_on_pid, [])
        # Threads that could be working on the request: not waiting for messages,
        # not themselves waiting on another server.
        workers = [s for s in server if s is not c and s.state != "RECEIVE"
                   and s.blocked_on_pid is None and s.priority is not None]
        if not workers:
            continue
        sp = max(s.priority for s in workers)
        if sp >= c.priority:
            continue
        ahead = [x for x in threads
                 if x.pid not in (c.pid, c.blocked_on_pid) and x.state in ("RUNNING", "READY")
                 and x.priority is not None and sp < x.priority < c.priority]
        top = max(workers, key=lambda s: s.priority)
        item = {
            "client": {"pid": c.pid, "tid": c.tid, "name": c.name, "thread_name": c.thread_name,
                       "priority": c.priority, "state": c.state},
            "server": {"pid": top.pid, "tid": top.tid, "name": top.name,
                       "thread_name": top.thread_name, "priority": sp, "state": top.state},
            "preempted_by": [{"pid": x.pid, "tid": x.tid, "name": x.name,
                              "thread_name": x.thread_name, "priority": x.priority,
                              "state": x.state} for x in ahead],
            "active": bool(ahead),
        }
        if c.blocked_ms is not None:
            item["blocked_ms"] = c.blocked_ms
        text = (f"{_label(c)} (priority {c.priority}) is waiting on {_label(top)}, which runs at "
                f"priority {sp}: the server did not inherit the client's priority.")
        if ahead:
            text += (" It is being held off by " +
                     ", ".join(f"{_label(x)} at priority {x.priority} ({x.state})" for x in ahead) +
                     ". This is a priority inversion. On QNX, servers normally inherit client "
                     "priority; check for _NTO_CHF_FIXED_PRIORITY on the server's channel.")
        item["explanation"] = text
        found.append(item)
    return found


def describe_deadlocks(cycles: list[list[int]], threads: list[Thread]) -> list[dict]:
    """Turn process cycles into readable deadlock reports."""
    out = []
    for cyc in cycles:
        members = [t for t in threads if t.pid in cyc and t.blocked_on_pid in cyc
                   and t.state in _PID_STATES]
        steps = [f"{_label(t)} is {t.state}-blocked on {t.blocked_on_pid}" for t in members]
        item = {
            "pids": cyc,
            "threads": [{"pid": t.pid, "tid": t.tid, "name": t.name, "thread_name": t.thread_name,
                         "state": t.state, "waiting_on": t.blocked_on_pid,
                         **({"blocked_ms": t.blocked_ms} if t.blocked_ms is not None else {})}
                        for t in members],
            "explanation": "Deadlock: " + "; ".join(steps) + ". Each is waiting for the other, "
                           "so none can move. A SEND-blocked thread means nobody on that side is "
                           "receiving; look for a server that sends to its own client.",
        }
        out.append(item)
    return out


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
