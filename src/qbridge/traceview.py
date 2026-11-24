"""Analyse a recorded QNX kernel trace on the host.

No target, no qbridged and no instrumented task: the input is the text that
the SDP's `traceprinter` prints for a `tracelogger` capture, from any program
(a `.kev` file is decoded with traceprinter first). The output is, per
thread, the time spent in each state, how long it waited for a CPU, and for
its longest waits who ran instead, plus patterns worth a closer look.

The patterns are hints, not diagnoses. A trace records every thread state
change, but not what a blocked thread waits for: THMUTEX, THSEND and THREPLY
name the waiting thread, not the mutex owner or the server. So "possible
priority inversion" lists threads below the waiter's priority that were held
off the CPU by a middle-priority thread while the waiter was blocked; which
of them it actually waited for takes a live snapshot (`qbridge diagnose
--trace`), which reads the mutex owner and the blocked-on server.
"""

from __future__ import annotations

import bisect
import gzip
import re
from collections import defaultdict
from pathlib import Path

from . import deadlines
from .parsers import kev

KERNEL_PID = 1                     # procnto: idle, clock, IPI and kernel threads
BLOCKED = ("MUTEX", "SEND", "REPLY")   # waiting for another thread to release or answer
WAITS = ("READY",) + BLOCKED
SHARE = 0.5                        # "most of": at least half
DEFAULT_THREADS = 6


# -- loading -----------------------------------------------------------------------

def load_text(path: str | Path) -> str:
    """traceprinter text from a .txt, .txt.gz or .kev file."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"no such trace file: {p}")
    data = p.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    elif p.suffix.lower() == ".kev":
        return _decode_kev(p)
    # traceprinter on Windows writes CRLF; the parser does not care, but keep one form.
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _decode_kev(p: Path) -> str:
    """Raw .kev files need the SDP's traceprinter (QNX does not publish the format)."""
    import subprocess
    import tempfile
    from . import tools
    exe = tools._traceprinter()
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "trace.txt"
        r = subprocess.run([exe, "-f", str(p), "-o", str(out)], capture_output=True, text=True)
        if r.returncode != 0 or not out.is_file():
            raise ValueError(f"traceprinter could not decode {p.name}: "
                             f"{(r.stderr or r.stdout).strip()[-300:]}")
        return out.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")


# -- index of who ran where -----------------------------------------------------------

class _RunIndex:
    """RUNNING spans per CPU, sorted, for fast "who ran on CPU c in [a, b]"."""

    def __init__(self, tr: kev.Trace):
        per_cpu: dict = defaultdict(list)
        for key, spans in tr.spans.items():
            for s in spans:
                if s.state == "RUNNING" and s.end > s.start:
                    per_cpu[s.cpu].append((s.start, s.end, key, s.priority))
        self.cpu = {c: sorted(v) for c, v in per_cpu.items()}
        self.starts = {c: [x[0] for x in v] for c, v in self.cpu.items()}

    def during(self, cpu: int, a: float, b: float):
        """(key, priority, ms) of each RUNNING piece on `cpu` overlapping [a, b]."""
        spans, starts = self.cpu.get(cpu, []), self.starts.get(cpu, [])
        # Spans on one CPU do not overlap, so only the one before `a` can reach into it.
        i = max(0, bisect.bisect_left(starts, a) - 1)
        while i < len(spans) and spans[i][0] < b:
            s, e, key, prio = spans[i]
            if e > a:
                yield key, prio, min(b, e) - max(a, s)
            i += 1


def _label(tr: kev.Trace, key) -> str:
    """tr.label, plus the thread id when another thread of the process has the same name."""
    base = tr.label(key)
    name = tr.thread_names.get(key)
    if name and sum(1 for k, n in tr.thread_names.items() if k[0] == key[0] and n == name) > 1:
        return f"{base} tid {key[1]}"
    return base


# -- per-thread report ------------------------------------------------------------------

def _pctl(values: list[float], q: float) -> float:
    v = sorted(values)
    return v[int((len(v) - 1) * q)] if v else 0.0


def _priorities(tr: kev.Trace, key) -> list[int]:
    seen: dict = defaultdict(float)
    for s in tr.spans.get(key, []):
        if s.state in ("RUNNING", "READY") and s.priority >= 0:
            seen[s.priority] += s.ms
    return sorted(seen, key=lambda p: -seen[p])


def thread_report(tr: kev.Trace, key: tuple[int, int], idx: _RunIndex | None = None) -> dict:
    """What a thread did in the trace, and who held the CPU while it waited."""
    idx = idx or _RunIndex(tr)
    spans = tr.spans.get(key, [])
    ready = [s.ms for s in spans if s.state == "READY"]
    out = {"thread": _label(tr, key), "pid": key[0], "tid": key[1],
           "priorities": _priorities(tr, key),
           "state_ms": {k: round(v, 2) for k, v in
                        sorted(tr.state_totals(key).items(), key=lambda kv: -kv[1])},
           "ready_waits": {"count": len(ready), "p50_ms": round(_pctl(ready, 0.5), 3),
                           "p99_ms": round(_pctl(ready, 0.99), 3),
                           "max_ms": round(max(ready, default=0.0), 3)},
           "longest_waits": []}
    for sp in sorted((s for s in spans if s.state in WAITS), key=lambda s: -s.ms)[:3]:
        w = {"state": sp.state, "start_ms": round(sp.start, 2), "ms": round(sp.ms, 2),
             "cpu": sp.cpu, "priority": sp.priority}
        if sp.state == "READY":
            # Waiting for its CPU: who had that CPU.
            ran: dict = defaultdict(float)
            for k, p, ms in idx.during(sp.cpu, sp.start, sp.end):
                if k != key:
                    ran[(k, p)] += ms
            w["ran_instead"] = [{"thread": _label(tr, k), "priority": p, "ms": round(ms, 2)}
                                for (k, p), ms in sorted(ran.items(), key=lambda kv: -kv[1])[:3]]
        else:
            # Blocked, so on no CPU: which lower-priority threads ran anywhere meanwhile.
            low: dict = defaultdict(float)
            for cpu in idx.cpu:
                for k, p, ms in idx.during(cpu, sp.start, sp.end):
                    if k != key and k[0] != KERNEL_PID and 0 <= p < sp.priority:
                        low[(k, p)] += ms
            w["lower_priority_ran"] = [{"thread": _label(tr, k), "priority": p, "ms": round(ms, 2)}
                                       for (k, p), ms in sorted(low.items(), key=lambda kv: -kv[1])[:3]]
        out["longest_waits"].append(w)
    return out


# -- patterns ---------------------------------------------------------------------------

def _main(ms_by: dict, n: int = 3) -> list:
    """The largest contributors, leaving out any under a tenth of the largest."""
    top = sorted(ms_by.items(), key=lambda kv: -kv[1])[:n]
    return [kv for kv in top if kv[1] >= 0.1 * top[0][1]] if top else []


def _preemption(tr: kev.Trace, key, idx: _RunIndex) -> dict | None:
    """Ready to run for a large part of the trace while higher-priority threads had its CPU."""
    ready = [s for s in tr.spans.get(key, []) if s.state == "READY"]
    ready_ms = sum(s.ms for s in ready)
    if ready_ms < max(1.0, 0.05 * tr.duration_ms):
        return None
    above: dict = defaultdict(float)
    for s in ready:
        for k, p, ms in idx.during(s.cpu, s.start, s.end):
            if k != key and p > s.priority:
                above[(k, p)] += ms
    above_ms = sum(above.values())
    if above_ms < SHARE * ready_ms:
        return None
    top = _main(above)
    return {
        "pattern": "cpu_preemption", "thread": _label(tr, key), "pid": key[0], "tid": key[1],
        "priority": _priorities(tr, key)[:1],
        "ready_ms": round(ready_ms, 2), "higher_priority_ran_ms": round(above_ms, 2),
        "ran_instead": [{"thread": _label(tr, k), "priority": p, "ms": round(ms, 2)}
                        for (k, p), ms in top],
        "text": (f"{_label(tr, key)} was ready to run but not running for {ready_ms:.1f} ms of the "
                 f"{tr.duration_ms:.0f} ms trace; higher-priority threads had its CPU for "
                 f"{above_ms:.1f} ms of that, mostly "
                 + ", ".join(f"{_label(tr, k)} (priority {p}, {ms:.1f} ms)" for (k, p), ms in top)
                 + ". If this thread has a deadline, its priority may be too low for this load."),
        "longest": max(ready, key=lambda s: s.ms),
    }


def _inversion(tr: kev.Trace, key, idx: _RunIndex) -> dict | None:
    """Blocked (mutex, send, reply) while a lower-priority thread was ready and a thread
    between the two priorities had that thread's CPU: the shape of a priority inversion,
    with the lower thread as a candidate for whoever the waiter was waiting for."""
    waits = [s for s in tr.spans.get(key, []) if s.state in BLOCKED and s.priority > 0]
    wait_ms = sum(s.ms for s in waits)
    if wait_ms < _min_ms(tr):
        return None
    cand: dict = {}    # low key -> {"ready", "ran": ms, "middle": {(k, p): ms}, "prio": p}
    for other, spans in tr.spans.items():
        if other == key or other[0] == KERNEL_PID:
            continue
        for rs in spans:
            if rs.state not in ("READY", "RUNNING"):
                continue
            for w in waits:
                a, b = max(rs.start, w.start), min(rs.end, w.end)
                if b <= a or rs.priority >= w.priority:
                    continue
                c = cand.setdefault(other, {"ready": 0.0, "ran": 0.0, "middle": defaultdict(float),
                                            "prio": rs.priority})
                if rs.state == "RUNNING":
                    c["ran"] += b - a       # e.g. a server doing the client's work
                    continue
                c["ready"] += b - a
                for k, p, ms in idx.during(rs.cpu, a, b):
                    if k not in (other, key) and k[0] != KERNEL_PID and rs.priority < p < w.priority:
                        c["middle"][(k, p)] += ms
    scored = []
    for low, c in cand.items():
        middle = sum(c["middle"].values())
        # Ready for most of the wait that it did not spend running, and held off
        # mostly by middle-priority threads.
        held_off = wait_ms - c["ran"]
        if (held_off > 0 and c["ready"] >= SHARE * held_off and middle >= SHARE * c["ready"]
                and middle >= 1.0):
            scored.append((middle, low, c))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    middle, low, c = scored[0]
    mids = _main(c["middle"])
    states = sorted({s.state for s in waits})
    prio = _priorities(tr, key)[:1]
    return {
        "pattern": "possible_priority_inversion", "thread": _label(tr, key), "pid": key[0],
        "tid": key[1], "priority": prio, "blocked_in": states, "blocked_ms": round(wait_ms, 2),
        "lower": {"thread": _label(tr, low), "pid": low[0], "tid": low[1], "priority": c["prio"],
                  "ready_ms": round(c["ready"], 2)},
        "middle": [{"thread": _label(tr, k), "pid": k[0], "tid": k[1], "priority": p,
                    "ms": round(ms, 2)} for (k, p), ms in mids],
        "other_candidates": [_label(tr, k) for _, k, _ in scored[1:4]],
        "text": (f"{_label(tr, key)}{' (priority ' + str(prio[0]) + ')' if prio else ''} was blocked "
                 f"({'/'.join(states)}) for {wait_ms:.1f} ms. Meanwhile {_label(tr, low)} "
                 f"(priority {c['prio']}) was ready but held off its CPU for {c['ready']:.1f} ms, "
                 f"mostly by "
                 + ", ".join(f"{_label(tr, k)} (priority {p}, {ms:.1f} ms)" for (k, p), ms in mids)
                 + ". If the blocked thread was waiting for that thread (a mutex it holds, or a "
                   "server answering its message), this is a priority inversion."),
        "cannot_tell": ("The trace does not say what a blocked thread waits for (THMUTEX, THSEND "
                        "and THREPLY name only the waiter), so the lower thread is a candidate, "
                        "not a proven holder. On a live target, 'qbridge diagnose --trace' reads "
                        "the mutex owner or the blocked-on server and checks it step by step."),
        "longest": max(waits, key=lambda s: s.ms),
        "lanes": [("blocked", key), ("lower", low)] + [("middle", k) for (k, _), _ in mids[:1]],
    }


def _min_ms(tr: kev.Trace) -> float:
    """Waits shorter than this (1% of the trace, at least 1 ms) are not reported as patterns."""
    return max(1.0, 0.01 * tr.duration_ms)


def _is_tracer(tr: kev.Trace, key) -> bool:
    """tracelogger itself: its waits are an effect of recording the trace."""
    return tr.proc_names.get(key[0], "").rsplit("/", 1)[-1] == "tracelogger"


def patterns(tr: kev.Trace, keys=None, idx: _RunIndex | None = None) -> list[dict]:
    idx = idx or _RunIndex(tr)
    keys = keys if keys is not None else [k for k in tr.spans
                                          if k[0] != KERNEL_PID and not _is_tracer(tr, k)]
    found = []
    for k in keys:
        for rule in (_inversion, _preemption):
            p = rule(tr, k, idx)
            if p:
                found.append(p)
    # A thread held off its CPU as the lower thread of an inversion is part of that
    # story; a separate preemption hint for it would only repeat it.
    lower = {(p["lower"]["pid"], p["lower"]["tid"]) for p in found if "lower" in p}
    found = [p for p in found
             if not (p["pattern"] == "cpu_preemption" and (p["pid"], p["tid"]) in lower)]
    # Highest-priority threads first: their waits matter most.
    found.sort(key=lambda p: (-(p["priority"][0] if p["priority"] else -1), p["pattern"]))
    return found


# -- choosing threads -------------------------------------------------------------------

def resolve(tr: kev.Trace, threads: list[str] | None = None, process: str | None = None):
    """Thread keys for names ("A-control"), "pid/tid" pairs, or every thread of a process
    (by name or pid). Returns (keys, not_found)."""
    keys, missing = [], []
    for spec in threads or []:
        m = re.fullmatch(r"(\d+)/(\d+)", spec.strip())
        if m:
            found = [(int(m[1]), int(m[2]))] if (int(m[1]), int(m[2])) in tr.spans else []
        else:
            found = sorted(k for k, n in tr.thread_names.items() if n == spec and k in tr.spans)
        keys += [k for k in found if k not in keys]
        if not found:
            missing.append(spec)
    if process:
        pids = {p for p, path in tr.proc_names.items()
                if path.rsplit("/", 1)[-1] == process or str(p) == process}
        found = sorted(k for k in tr.spans if k[0] in pids)
        keys += [k for k in found if k not in keys]
        if not found:
            missing.append(process)
    return keys, missing


def _default_keys(tr: kev.Trace, found: list[dict]) -> list:
    """Threads with a pattern first, then the ones that waited longest."""
    keys = []
    for p in found:
        for k in [(p["pid"], p["tid"])] + [(x["pid"], x["tid"]) for x in
                                           ([p["lower"]] if "lower" in p else []) + p.get("middle", [])[:1]]:
            if k not in keys:
                keys.append(k)
    # Then the longest waits for a CPU or a lock. Long SEND/REPLY waits are left out
    # here: a shell or client waiting for input sits in REPLY for the whole trace.
    waited = sorted(((sum(s.ms for s in v if s.state in ("READY", "MUTEX")), k)
                     for k, v in tr.spans.items()
                     if k[0] != KERNEL_PID and not _is_tracer(tr, k)), reverse=True)
    for ms, k in waited:
        if len(keys) >= DEFAULT_THREADS or ms < 1.0:
            break
        if k not in keys:
            keys.append(k)
    return keys


# -- whole analysis ---------------------------------------------------------------------

def _deadline_findings(tr: kev.Trace, c: dict, found: list[dict]) -> list[dict]:
    """Run the deadline-report rules on a periodic thread's rebuilt cycles. A hint that
    another thread held it up (preemption, possible inversion) counts as another finding,
    so the late wake-up and overrun rules stay quiet when that explains the misses."""
    from . import analysis
    others = [{"rule": analysis.RULE_CPU_PREEMPTION} for p in found
              if (p["pid"], p["tid"]) == (c["pid"], c["tid"])]
    out = []
    for rule in (analysis.late_wakeup, analysis.own_work_overrun):
        for f in rule([], c["report"], None, others):
            f["subject"]["pid"], f["subject"]["task"]["tid"] = c["pid"], c["tid"]
            f["could_not_determine"].append("These cycles were rebuilt from the kernel trace, "
                                            "not reported by the program (see the limits).")
            out.append(f)
    return out


def _periodic(tr: kev.Trace, chosen: list, found: list[dict], period_ms: float | None,
              deadline_ms: float | None) -> list[dict]:
    from . import tracecycles
    if chosen:
        got = [tracecycles.cycles(tr, k, period_ms, deadline_ms) for k in chosen]
        got = [c for c in got if c]
    else:
        got = tracecycles.periodic_threads(tr)
    for c in got:
        c["thread"] = _label(tr, (c["pid"], c["tid"]))
        c["findings"] = _deadline_findings(tr, c, found)
    return got


def analyse(tr: kev.Trace, threads: list[str] | None = None, process: str | None = None,
            period_ms: float | None = None, deadline_ms: float | None = None) -> dict:
    idx = _RunIndex(tr)
    chosen, missing = resolve(tr, threads, process)
    if (period_ms or deadline_ms) and not chosen:
        raise ValueError("a period or deadline needs the thread it belongs to (--thread)")
    found = patterns(tr, chosen or None, idx)
    periodic = _periodic(tr, chosen, found, period_ms, deadline_ms)
    keys = chosen or _default_keys(tr, found)
    for c in periodic:                       # periodic threads that missed deadlines too
        k = (c["pid"], c["tid"])
        if not chosen and c["report"]["misses"] and k not in keys:
            keys.append(k)
    busiest = sorted(((tr.state_totals(k).get("RUNNING", 0.0), k) for k in tr.spans
                      if not tr.thread_names.get(k, "").startswith("idle")), reverse=True)[:8]
    h = tr.header
    return {
        "trace": {"duration_ms": round(tr.duration_ms, 1), "events": sum(tr.event_counts.values()),
                  "cpus": int(h.get("TRACE_CPU_NUM", "0") or 0) or len(idx.cpu),
                  "system": " ".join(x for x in (h.get("TRACE_SYSNAME"), h.get("TRACE_SYS_RELEASE"),
                                                 h.get("TRACE_MACHINE")) if x),
                  "date": h.get("TRACE_DATE"), "processes": len(tr.proc_names),
                  "threads": len(tr.spans)},
        "selected": "named" if chosen else "automatic (threads with a pattern, then the longest waits)",
        "patterns": [{k: v for k, v in p.items() if k not in ("longest", "lanes")} for p in found],
        "periodic": periodic,
        "threads": [thread_report(tr, k, idx) for k in keys],
        "busiest": [{"thread": _label(tr, k), "running_ms": round(ms, 2)} for ms, k in busiest if ms > 0],
        "not_found": missing,
        "limits": [
            f"The trace covers {tr.duration_ms:.0f} ms; it shows what happened in that window only.",
            "Patterns are hints from thread states and priorities. The trace does not record what "
            "a blocked thread waits for.",
            "Deadline numbers for periodic threads are rebuilt from the trace: the period is found "
            "from the wake-ups and the deadline is taken as the period unless given. Checked "
            "against rtdemo's own per-cycle log on Nov 23: the same cycles missed in three runs "
            "of about 200 cycles each.",
        ],
        "_found": found,          # for the HTML page; removed before printing
    }


# -- Perfetto export ----------------------------------------------------------------------

CPU_PID = 0          # no QNX process has pid 0; the per-CPU tracks go here
_SKIP = ("CREATE", "DEAD", "DESTROY")   # zero-length bookkeeping states


def _us(ms: float) -> float:
    return round(ms * 1000, 3)


def perfetto(tr: kev.Trace, found: list[dict] | None = None) -> dict:
    """The trace in Chrome Trace Event JSON, which ui.perfetto.dev and chrome://tracing open.

    One track per thread (a slice per state, with priority and CPU as arguments), grouped
    by process; a "CPUs" group with one track per CPU showing which thread ran there; and
    a marker for each pattern hint. Times are microseconds from the start of the trace."""
    ev: list[dict] = []
    ev.append({"ph": "M", "name": "process_name", "pid": CPU_PID, "args": {"name": "CPUs"}})
    ev.append({"ph": "M", "name": "process_sort_index", "pid": CPU_PID, "args": {"sort_index": -1}})
    cpus = sorted({s.cpu for v in tr.spans.values() for s in v})
    for c in cpus:
        ev.append({"ph": "M", "name": "thread_name", "pid": CPU_PID, "tid": c,
                   "args": {"name": f"CPU {c}"}})
    # Processes named in a hint first, then the busiest, so the story is at the top.
    first: list[int] = []
    for p in found or []:
        for pid in [p["pid"], p.get("lower", {}).get("pid")] + [m["pid"] for m in p.get("middle", [])]:
            if pid is not None and pid not in first:
                first.append(pid)
    busy: dict = defaultdict(float)
    for k, v in tr.spans.items():
        busy[k[0]] += sum(s.ms for s in v if s.state == "RUNNING")
    pids = first + sorted({k[0] for k in tr.spans} - set(first), key=lambda p: -busy[p])
    for i, pid in enumerate(pids):
        name = tr.proc_names.get(pid, "").rsplit("/", 1)[-1] or "?"
        ev.append({"ph": "M", "name": "process_name", "pid": pid, "args": {"name": f"{name} [{pid}]"}})
        ev.append({"ph": "M", "name": "process_sort_index", "pid": pid, "args": {"sort_index": i}})
    for key in sorted(tr.spans):
        name = tr.thread_names.get(key) or f"tid {key[1]}"
        ev.append({"ph": "M", "name": "thread_name", "pid": key[0], "tid": key[1],
                   "args": {"name": name}})
        for s in tr.spans[key]:
            if s.state in _SKIP or s.end <= s.start:
                continue
            # Round the start and the end (not the length), so that back-to-back
            # slices stay back to back; Perfetto rejects overlaps on one track.
            ts, end = _us(s.start), _us(s.end)
            ev.append({"ph": "X", "cat": "state", "name": s.state, "pid": key[0], "tid": key[1],
                       "ts": ts, "dur": round(end - ts, 3),
                       "args": {"priority": s.priority, "cpu": s.cpu}})
            if s.state == "RUNNING":
                ev.append({"ph": "X", "cat": "cpu", "name": _label(tr, key), "pid": CPU_PID,
                           "tid": s.cpu, "ts": ts, "dur": round(end - ts, 3),
                           "args": {"priority": s.priority, "pid": key[0], "tid": key[1]}})
    for p in found or []:
        title = ("possible priority inversion" if p["pattern"] == "possible_priority_inversion"
                 else "CPU preemption")
        ev.append({"ph": "i", "s": "t", "cat": "hint", "name": f"hint: {title}",
                   "pid": p["pid"], "tid": p["tid"], "ts": _us(p["longest"].start),
                   "args": {"text": p["text"], **({"cannot_tell": p["cannot_tell"]}
                                                  if p.get("cannot_tell") else {})}})
    h = tr.header
    return {"traceEvents": ev, "displayTimeUnit": "ms",
            "otherData": {"source": "qbridge trace --file",
                          "system": " ".join(x for x in (h.get("TRACE_SYSNAME"), h.get("TRACE_SYS_RELEASE"),
                                                         h.get("TRACE_MACHINE")) if x),
                          "recorded": h.get("TRACE_DATE", ""),
                          "cpus": len(cpus), "duration_ms": round(tr.duration_ms, 3)}}


# -- HTML page ----------------------------------------------------------------------------

def _timeline_for(tr: kev.Trace, p: dict) -> dict | None:
    from . import traceconfirm
    if p["pattern"] == "possible_priority_inversion":
        lanes = p["lanes"]
    else:
        lanes = [("ready", (p["pid"], p["tid"]))]
        lanes += [("ran instead", k) for k in _keys_by_label(tr, [x["thread"] for x in p["ran_instead"][:2]])]
    return traceconfirm._timeline(tr, lanes, p["longest"])


def _keys_by_label(tr: kev.Trace, labels: list[str]) -> list:
    by = {_label(tr, k): k for k in tr.spans}
    return [by[l] for l in labels if l in by]


def html(result: dict, tr: kev.Trace, source: str) -> str:
    from html import escape as e_
    from . import report
    t = result["trace"]
    parts = [f"""
<header><p class="eyebrow">qbridge RT · kernel trace</p>
<h1>{e_(Path(source).name)}</h1>
<p class="meta">{e_(t['system'] or 'QNX')} · {t['cpus']} CPU(s) · {t['duration_ms']} ms ·
{t['events']} events · {t['processes']} processes, {t['threads']} threads{' · recorded ' + e_(t['date']) if t.get('date') else ''}</p>
</header>"""]
    found = result.get("_found", [])
    if found:
        items = []
        for p in found:
            title = ("Possible priority inversion" if p["pattern"] == "possible_priority_inversion"
                     else "Waiting for the CPU behind higher-priority threads")
            tl = _timeline_for(tr, p)
            items.append(f'<div class="finding"><div class="finding-head"><h3>{e_(title)}</h3>'
                         f'<span class="conf c-low">hint</span></div><p>{e_(p["text"])}</p>'
                         + (f'<p class="muted">{e_(p["cannot_tell"])}</p>' if p.get("cannot_tell") else "")
                         + (report._trace_timeline(tl) if tl else "") + "</div>")
        parts.append("<section><h2>Patterns worth a closer look</h2>" + "".join(items) + "</section>")
    else:
        parts.append("<section><h2>Patterns worth a closer look</h2><p>None found: no selected "
                     "thread waited long for its CPU behind higher-priority threads, or was "
                     "blocked while a lower-priority thread was held off its CPU.</p></section>")
    per = result.get("periodic") or []
    if per:
        prow = []
        for c in per:
            r = c["report"]
            roots = ", ".join(f"{deadlines._CAUSE_TEXT.get(k, k)} ({n})"
                              for k, n in sorted(r["chain_root_causes"].items(), key=lambda kv: -kv[1]))
            prow.append(f"<tr><td>{e_(c['thread'])}</td><td class='num'>{c['period_ms']:.2f}</td>"
                        f"<td class='num'>{c['cycles']}</td>"
                        f"<td class='num'>{r['misses']} ({r['miss_rate']:.0%})</td>"
                        f"<td class='num'>{r['response_us']['p50'] / 1000:.2f}</td>"
                        f"<td class='num'>{r['response_us']['max'] / 1000:.1f}</td>"
                        f"<td style='white-space:normal;min-width:180px'>{e_(roots or '-')}</td>"
                        f"<td class='num'>{c['estimated_cycles']}</td></tr>")
        notes = "".join(f"<li><strong>{e_(f['title'])}</strong> ({e_(f['confidence'])}): "
                        f"{e_(f['explanation'])}</li>" for c in per for f in c.get("findings", []))
        parts.append(f"""
<section><h2>Periodic threads and their deadlines</h2>
<p class="muted">Rebuilt from the trace alone: the period is found from each thread's wake-ups, and the
deadline is taken as the period unless given. Times in ms. Median and worst are response times. "Est." counts cycles that ran back to back
after an overrun, whose boundaries are not in the trace.</p>
<div class="table-wrap"><table><thead><tr><th>Thread</th><th>Period</th><th>Cycles</th><th>Missed</th>
<th>Median</th><th>Worst</th><th>What started each run of misses</th><th>Est.</th></tr></thead>
<tbody>{''.join(prow)}</tbody></table></div>{('<ul>' + notes + '</ul>') if notes else ''}
</section>""")
    rows = []
    for th in result["threads"]:
        st = th["state_ms"]
        rw = th["ready_waits"]
        rows.append(f"<tr><td>{e_(th['thread'])}</td><td class='num'>{e_(', '.join(map(str, th['priorities'][:2])))}</td>"
                    + "".join(f"<td class='num'>{st.get(x, 0):.1f}</td>" for x in ("RUNNING", "READY", "MUTEX"))
                    + f"<td class='num'>{st.get('SEND', 0) + st.get('REPLY', 0):.1f}</td>"
                    f"<td class='num'>{rw['max_ms']:.2f}</td></tr>")
    parts.append(f"""
<section><h2>Threads</h2><p class="muted">Selection: {e_(result['selected'])}. Times in ms.</p>
<div class="table-wrap"><table><thead><tr><th>Thread</th><th>Priority</th><th>Running</th><th>Ready</th>
<th>Mutex</th><th>Send/Reply</th><th>Longest ready wait</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</section>""")
    parts.append("<section><h2>Limitations</h2><ul>" + "".join(f"<li>{e_(x)}</li>" for x in result["limits"])
                 + "</ul></section>")
    return report._PAGE.replace("{{TITLE}}", e_(f"qbridge RT trace: {Path(source).name}")).replace(
        "{{BODY}}", "".join(parts))
