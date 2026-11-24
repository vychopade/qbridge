"""Parse QNX kernel trace output (traceprinter text) into thread state spans.

A trace is captured on the target with `tracelogger` (instrumented kernel
procnto-smp-instr) and decoded on the host with the SDP's `traceprinter`.
traceprinter prints one event per line, e.g. (checked on QNX 8.0):

    t:0xd36a69f5 CPU:01 THREAD          :THRUNNING          pid:1 tid:4 priority:255 policy:8
    t:0xd36e65bf CPU:00 PROCESS         :PROCCREATE_NAME
                          ppid:8044570
                           pid:8060930
                          name:data/qbridge/rtdemo

`t` is only the low 32 bits of the CPU cycle counter. CONTROL:TIME events
carry the high bits ("msb") for each CPU; between them the low bits wrap
roughly every second at 3.9 GHz, which is detected per CPU. Times are turned
into milliseconds with TRACE_CYCLES_PER_SEC from the header.

THREAD class events (THRUNNING, THREADY, THMUTEX, THNANOSLEEP, ...) mark the
moment a thread enters that state; a state lasts until the thread's next
THREAD event. That gives, per thread, a list of spans.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

_HEAD = re.compile(r"^t:0x([0-9a-f]+) CPU:(\d+) (\w+)\s+:(\w+)\s*(.*)$")
_FIELD = re.compile(r"(\w+):(\S+)")
_NAME = re.compile(r"^\s+name:(.*)$")
_EQ = re.compile(r"(\w+) = ([^,\s]+)")

# Kernel calls kept for linking threads (kevdeps.py).
LINK_CALLS = {"SYNC_MUTEX_LOCK", "SYNC_MUTEX_UNLOCK", "SYNC_CONDVAR_WAIT", "SYNC_CONDVAR_SIGNAL",
              "SYNC_SEM_WAIT", "SYNC_SEM_POST", "MSG_SENDV", "MSG_SENDVNC", "MSG_RECEIVEV",
              "MSG_REPLYV", "MSG_ERROR", "SCHED_SET", "SYNC_MUTEX_REVIVE"}


@dataclass
class Span:
    state: str          # RUNNING, READY, MUTEX, NANOSLEEP, ...
    start: float        # ms from the start of the trace
    end: float
    cpu: int
    priority: int

    @property
    def ms(self) -> float:
        return self.end - self.start


@dataclass
class Trace:
    header: dict
    cycles_per_sec: int
    duration_ms: float
    proc_names: dict = field(default_factory=dict)       # pid -> path
    thread_names: dict = field(default_factory=dict)     # (pid, tid) -> name
    spans: dict = field(default_factory=lambda: defaultdict(list))   # (pid, tid) -> [Span]
    event_counts: dict = field(default_factory=dict)
    # Kernel calls and communication events that link threads (mutex owner, message
    # client/server); present only if those classes were traced. Each has "by": the
    # thread RUNNING on that CPU when it was logged (the caller), if known.
    links: list = field(default_factory=list)
    # CONTROL DROPPED: {"t" (ms, when tracing resumed), "cpu", "count"}
    drops: list = field(default_factory=list)
    decoder: str = "traceprinter text"
    decode_stats: dict = field(default_factory=dict)
    decode_problems: list = field(default_factory=list)

    def find_thread(self, name: str, pid: int | None = None) -> tuple[int, int] | None:
        for k, v in self.thread_names.items():
            if v == name and (pid is None or k[0] == pid):
                return k
        return None

    def label(self, key: tuple[int, int]) -> str:
        proc = self.proc_names.get(key[0], "?").rsplit("/", 1)[-1]
        name = self.thread_names.get(key)
        return f"{proc}[{key[0]}] '{name}'" if name else f"{proc}[{key[0]}] tid {key[1]}"

    def state_totals(self, key: tuple[int, int]) -> dict:
        out: dict = defaultdict(float)
        for s in self.spans.get(key, []):
            out[s.state] += s.ms
        return dict(out)

    def running_during(self, cpu: int, start: float, end: float,
                       exclude: tuple[int, int] | None = None) -> dict:
        """ms each thread spent RUNNING on `cpu` within [start, end] -> {(key, prio): ms}."""
        out: dict = defaultdict(float)
        for key, spans in self.spans.items():
            if key == exclude:
                continue
            for s in spans:
                if s.state == "RUNNING" and s.cpu == cpu and s.end > start and s.start < end:
                    out[(key, s.priority)] += min(end, s.end) - max(start, s.start)
        return dict(out)

    def states_during(self, key: tuple[int, int], start: float, end: float) -> dict:
        """ms the thread spent in each (state, priority) within [start, end]."""
        out: dict = defaultdict(float)
        for s in self.spans.get(key, []):
            if s.end > start and s.start < end:
                out[(s.state, s.priority)] += min(end, s.end) - max(start, s.start)
        return dict(out)


def events_from_text(text: str) -> tuple[dict, int, list]:
    """traceprinter text -> (header, cycles per second, events sorted by time)."""
    header = dict(re.findall(r"^\s*(TRACE_\w+):: (.*)$", text, re.M))
    cps = int(header.get("TRACE_CYCLES_PER_SEC", "0") or 0)
    if cps <= 0:
        raise ValueError("not a traceprinter output: TRACE_CYCLES_PER_SEC missing")

    events = []
    cur = None
    msb: dict = defaultdict(int)
    last: dict = {}
    for line in text.splitlines():
        m = _HEAD.match(line)
        if m:
            lsb, cpu, cls, ev, rest = int(m[1], 16), int(m[2]), m[3], m[4], m[5]
            if cls == "CONTROL" and ev == "TIME":
                mm = re.search(r"msb:0x([0-9a-f]+)", rest)
                if mm:
                    msb[cpu] = int(mm[1], 16)
            elif cpu in last and lsb < last[cpu] and last[cpu] - lsb > 0x80000000:
                msb[cpu] += 1                       # the low 32 bits wrapped
            last[cpu] = lsb
            cur = {"t": (msb[cpu] << 32) | lsb, "cpu": cpu, "cls": cls, "ev": ev,
                   "f": dict(_FIELD.findall(rest))}
            cur["f"].update(_EQ.findall(rest))          # "dropped_events = 15224"
            events.append(cur)
        elif cur is not None and line.startswith(" "):
            nm = _NAME.match(line)
            if nm:                                  # names may contain spaces
                cur["f"]["name"] = nm[1].strip()
            else:
                cur["f"].update(_FIELD.findall(line))
    if not events:
        raise ValueError("no events in trace")
    events.sort(key=lambda e: e["t"])
    return header, cps, events


def parse(text: str) -> Trace:
    """traceprinter text -> Trace."""
    header, cps, events = events_from_text(text)
    tr = build(header, cps, events)
    tr.decoder = "traceprinter text"
    return tr


def parse_kev(data: bytes) -> Trace:
    """A raw .kev file -> Trace, decoded natively (kevbin), no SDP needed."""
    from . import kevbin
    kf = kevbin.decode(data, keep_words=False)
    if not kf.events:
        raise ValueError("no events in trace")
    header = {f"TRACE_{k}": v for k, v in kf.header.items()}
    tr = build(header, kf.cycles_per_sec, kf.events)
    tr.decoder = "qbridge native .kev decoder"
    tr.decode_stats = {k: v for k, v in kf.stats.items() if k != "by_type"}
    tr.decode_problems = [f"offset {o}: {t}" for o, t in kf.problems]
    return tr


def build(header: dict, cps: int, events: list) -> Trace:
    """Events (sorted by time) -> per-thread state spans, names, and the kernel call
    and communication events used to link threads (kevdeps.py)."""
    t0 = events[0]["t"]

    def ms(t: int) -> float:
        return (t - t0) / cps * 1000.0

    tr = Trace(header=header, cycles_per_sec=cps, duration_ms=ms(events[-1]["t"]))
    counts: dict = defaultdict(int)
    state: dict = {}
    running: dict = {}                       # cpu -> (pid, tid) now RUNNING there
    for e in events:
        ev = e["ev"]
        counts[f"{e['cls']}:{ev}"] += 1
        f = e["f"]
        cls = e["cls"]
        if ev == "PROCCREATE_NAME" and "pid" in f:
            tr.proc_names[int(f["pid"])] = f.get("name", "")
        elif ev == "PROCTHREAD_NAME" and "pid" in f and "tid" in f:
            tr.thread_names[(int(f["pid"]), int(f["tid"]))] = f.get("name", "")
        elif cls == "THREAD" and ev.startswith("TH") and "pid" in f and "tid" in f:
            key = (int(f["pid"]), int(f["tid"]))
            if key in state:
                st, start, cpu, prio = state[key]
                tr.spans[key].append(Span(st, ms(start), ms(e["t"]), cpu, prio))
            try:
                prio = int(f.get("priority", "-1"))
            except ValueError:
                prio = -1
            state[key] = (ev[2:], e["t"], e["cpu"], prio)
            if ev == "THRUNNING":
                running[e["cpu"]] = key
            elif running.get(e["cpu"]) == key:
                running.pop(e["cpu"], None)
        elif cls == "CONTROL" and ev == "DROPPED":
            tr.drops.append({"t": ms(e["t"]), "cpu": e["cpu"],
                             "count": int(f.get("dropped_events", "0") or 0)})
        elif cls in ("KER_CALL", "KER_EXIT", "COMM") and f:
            name = ev.split("/")[0]
            if cls == "COMM" or name in LINK_CALLS:
                tr.links.append({"t": ms(e["t"]), "cpu": e["cpu"], "cls": cls, "ev": name,
                                 "f": dict(f), "by": running.get(e["cpu"]),
                                 "off": e.get("off")})
    end = events[-1]["t"]
    for key, (st, start, cpu, prio) in state.items():      # close open spans at the end
        if end > start:
            tr.spans[key].append(Span(st, ms(start), ms(end), cpu, prio))
    tr.event_counts = dict(counts)
    return tr


def filter_text(text: str, pids: set[int], cpu: int | None = None) -> str:
    """Shrink traceprinter output for a test fixture: keep the header, CONTROL
    events, names, every event of `pids`, and THREAD events on `cpu` (to keep
    who ran there)."""
    out, keep, events = [], False, False
    for line in text.splitlines():
        m = _HEAD.match(line)
        if not m:
            if not events or (keep and line.startswith(" ")):
                out.append(line)          # the header, or a kept event's continuation
            continue
        events = True
        cpu_, cls, ev, rest = int(m[2]), m[3], m[4], m[5]
        f = dict(_FIELD.findall(rest))
        pid = int(f["pid"]) if f.get("pid", "").isdigit() else None
        keep = (cls == "CONTROL" or ev in ("PROCCREATE_NAME", "PROCTHREAD_NAME")
                or pid in pids or (cls == "THREAD" and cpu is not None and cpu_ == cpu))
        if keep:
            out.append(line)
    return "\n".join(out) + "\n"
