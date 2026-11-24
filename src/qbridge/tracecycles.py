"""Deadline statistics for an unmodified program, from a kernel trace alone.

qbridge's deadline numbers normally come from the task itself (rtdemo's samples,
or a task that reports to qbridged with qbridge_task.h). This module gets them
from a recorded kernel trace instead, for any periodic thread, with no change
to the program:

1. A thread's timeline is cut into busy intervals: from waking up to its next
   sleep (NANOSLEEP, RECEIVE, SIGWAITINFO, ...). MUTEX, SEND and REPLY are not
   sleeps: a thread blocked on a lock or a server is in the middle of a cycle.
2. The period is the spacing that the wake-ups fall on, allowing for skipped
   wake-ups (a cycle that overran runs the next ones without sleeping).
3. Releases are placed on that period grid, phased so the on-time wake-ups sit
   on it. Sleeping past a release is a late wake-up, which the trace shows.
4. A busy interval that covers several releases holds several cycles run back
   to back. The trace has no event at their boundaries, so they are estimated:
   each cycle is given the thread's median CPU time per cycle, counted back
   from the end of the interval. Those cycles are flagged as estimated.
5. Each cycle becomes a deadlines.Sample (release, response, late start, lock
   wait, wait for a server, CPU time) and goes through the same deadline
   analysis as measured samples.

What it cannot know: the program's real period and deadline (the deadline is
taken as the period unless given), whether a thread is meant to be periodic,
and the exact boundaries inside an overrun (see 4).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import deadlines
from .parsers import kev

# States a periodic thread waits in between cycles. A blocked state that waits for
# another thread to act (MUTEX, SEND, REPLY, JOIN) is part of a cycle.
IDLE = {"NANOSLEEP", "RECEIVE", "SIGWAITINFO", "SIGSUSPEND", "SEM", "CONDVAR", "INTR",
        "NET_REPLY", "NET_SEND", "STOPPED", "WAITCTX", "WAITPAGE", "WAITTHREAD"}
IGNORE = {"CREATE", "DEAD", "DESTROY"}
MIN_WAKES = 8           # fewer wake-ups than this: too few to call it periodic
REGULAR = 0.8           # share of wake-up gaps that must sit on the period grid
TOLERANCE = 0.15        # ... within this fraction of the period


@dataclass
class Busy:
    wake: float          # ms: left the idle state
    end: float           # ms: entered the idle state again
    spans: list


def busy_intervals(tr: kev.Trace, key) -> list[Busy]:
    """Complete busy intervals (both ends inside the trace)."""
    out, cur, seen_idle = [], None, False
    for s in tr.spans.get(key, []):
        if s.state in IGNORE:
            continue
        if s.state in IDLE:
            if cur is not None:
                cur.end = s.start
                out.append(cur)
                cur = None
            seen_idle = True
        elif cur is None and seen_idle:          # first busy span after an idle one
            cur = Busy(s.start, s.end, [s])
        elif cur is not None:
            cur.spans.append(s)
    return out                                   # an interval still open at the end is cut off


def _on_grid(gaps: list[float], p: float) -> float:
    ok = 0
    for g in gaps:
        k = max(1, round(g / p))
        if abs(g - k * p) <= TOLERANCE * p:
            ok += 1
    return ok / len(gaps) if gaps else 0.0


def find_period(wakes: list[float], sleeps: list[float] | None = None) -> tuple[float, float] | None:
    """(period ms, share of gaps on the grid), or None if the wake-ups are not periodic.

    A fraction of the period (P/2, P/3) fits the wake-ups at least as well as P, so
    the sleeps decide: a thread on a periodic timer wakes at the first release after
    it goes to sleep, so its sleeps are no longer than one period. A candidate that
    most sleeps outlast is a fraction of the real period."""
    if len(wakes) < MIN_WAKES:
        return None
    gaps = [b - a for a, b in zip(wakes, wakes[1:]) if b > a]
    srt = sorted(gaps)
    cands = sorted(set(round(c, 3) for c in srt[: max(1, int(len(srt) * 0.6))] if c > 0.05))
    best = None
    for c in cands:
        score = _on_grid(gaps, c)
        if score < REGULAR:
            continue
        if sleeps and sum(1 for s in sleeps if s <= c * (1 + TOLERANCE)) < REGULAR * len(sleeps):
            continue                       # most sleeps are longer than c: c is too short
        if best is None or score > best[1] + 1e-9 or (abs(score - best[1]) <= 1e-9 and c > best[0]):
            best = (c, score)
    if best is None:
        return None
    p0 = best[0]
    # Refine: each on-grid gap divided by its number of periods.
    per = [g / max(1, round(g / p0)) for g in gaps if abs(g - max(1, round(g / p0)) * p0) <= TOLERANCE * p0]
    per.sort()
    return per[len(per) // 2], best[1]


def _phase(wakes: list[float], p: float) -> float:
    """Offset of the release grid: the on-time wake-ups sit on it, late ones after it."""
    angles = [2 * math.pi * (w % p) / p for w in wakes]
    c = (math.atan2(sum(math.sin(a) for a in angles), sum(math.cos(a) for a in angles))
         / (2 * math.pi) * p) % p
    # Wake-ups can be late but not early: allow 10% of a period of jitter before the
    # typical wake-up and count everything after it as late (a symmetric window would
    # read a wake-up half a period late as one half a period early).
    d = sorted(((w % p) - c + 0.1 * p) % p - 0.1 * p for w in wakes)
    return (c + d[int(0.05 * (len(d) - 1))]) % p


def _state_ms(spans: list, a: float, b: float) -> dict:
    out: dict = {}
    for s in spans:
        lo, hi = max(a, s.start), min(b, s.end)
        if hi > lo:
            out[s.state] = out.get(s.state, 0.0) + hi - lo
    return out


def _first_run(spans: list, a: float) -> float | None:
    for s in spans:
        if s.state == "RUNNING" and s.end > a:
            return max(a, s.start)
    return None


def _boundaries(spans: list, wake: float, end: float, n: int, per_cycle: float) -> list[float]:
    """End times of n back-to-back cycles in [wake, end]: the last ends at `end`, each
    earlier one `per_cycle` ms of RUNNING time before the next."""
    runs = [(max(wake, s.start), min(end, s.end)) for s in spans
            if s.state == "RUNNING" and s.end > wake and s.start < end]
    ends = [end]
    need = per_cycle
    i = len(runs) - 1
    t_hi = end
    while len(ends) < n:
        while i >= 0 and runs[i][0] >= t_hi:
            i -= 1
        if i < 0:
            ends.append(wake)            # not enough CPU time recorded: collapse to the wake
            continue
        a, b = runs[i]
        b = min(b, t_hi)
        if b - a >= need:
            t = b - need
            ends.append(t)
            t_hi, need = t, per_cycle
        else:
            need -= b - a
            t_hi = a
            i -= 1
    return sorted(ends)


def cycles(tr: kev.Trace, key, period_ms: float | None = None,
           deadline_ms: float | None = None, keep_samples: bool = False) -> dict | None:
    """Deadline report for one thread from the trace, or None if it is not periodic."""
    busy = busy_intervals(tr, key)
    wakes = [b.wake for b in busy]
    sleeps = [nxt.wake - cur.end for cur, nxt in zip(busy, busy[1:])]
    found = None if period_ms else find_period(wakes, sleeps)
    if not period_ms and not found:
        return None
    p = period_ms or found[0]
    regular = found[1] if found else _on_grid([b - a for a, b in zip(wakes, wakes[1:])], p)
    deadline = deadline_ms or p
    phase = _phase(wakes, p)

    single = []
    for b in busy:
        r0 = math.floor((b.wake - phase) / p + 0.02) * p + phase
        if b.end - r0 <= p * (1 + TOLERANCE):
            single.append(sum(v for k, v in _state_ms(b.spans, b.wake, b.end).items() if k == "RUNNING"))
    single.sort()
    per_cycle = single[len(single) // 2] if single else None

    samples: list[deadlines.Sample] = []
    estimated = 0
    prev_end = None
    for b in busy:
        r0 = math.floor((b.wake - phase) / p + 0.02) * p + phase   # 2% slack for an early-looking wake
        if prev_end is not None:
            # On a periodic timer a thread wakes at the first release after it went to
            # sleep; a release it slept through is the one it woke up late for.
            r0 = min(r0, math.ceil((prev_end - phase) / p - 1e-9) * p + phase)
        prev_end = b.end
        n = max(1, math.ceil((b.end - r0) / p - 1e-9))
        n = min(n, int((b.end - r0) // p) + 1)
        if n > 1 and per_cycle:
            ends = _boundaries(b.spans, b.wake, b.end, n, per_cycle)
            estimated += n - 1
        else:
            n, ends = 1, [b.end]
        last = None
        for j, c_end in enumerate(ends):
            rel = r0 + j * p
            start = b.wake if j == 0 else last
            st = _state_ms(b.spans, start, c_end)
            if j == 0:
                fr = _first_run(b.spans, b.wake)
                latency = max(0.0, (fr if fr is not None else b.wake) - rel)
                backlog = 0.0
            else:
                latency = 0.0
                backlog = max(0.0, start - rel)
            samples.append(deadlines.Sample(
                release=rel * 1000, response=max(0.0, c_end - rel) * 1000,
                backlog=backlog * 1000, latency=latency * 1000,
                lock_wait=st.get("MUTEX", 0.0) * 1000,
                server_wait=(st.get("SEND", 0.0) + st.get("REPLY", 0.0)) * 1000,
                exec=st.get("RUNNING", 0.0) * 1000))
            last = c_end
    samples.sort(key=lambda s: s.release)
    name = tr.thread_names.get(key) or f"tid {key[1]}"
    rep = deadlines.analyse(samples, deadline * 1000, p * 1000, name)
    extra = {"samples": samples} if keep_samples else {}
    return {**extra,
        "thread": tr.label(key), "pid": key[0], "tid": key[1],
        "period_ms": round(p, 4), "period_source": "given" if period_ms else "found in the trace",
        "deadline_ms": round(deadline, 4),
        "deadline_source": "given" if deadline_ms else "taken as the period",
        "regularity": round(regular, 3), "cycles": len(samples), "estimated_cycles": estimated,
        "per_cycle_cpu_ms": round(per_cycle, 4) if per_cycle else None,
        "report": rep, "summary": rep["summary"],
        "limits": [
            "Releases are placed on the period grid found in the trace; the program's own "
            "timer settings are not known.",
            ("The deadline is taken to be the period; pass the real one if it differs."
             if not deadline_ms else "The deadline was given."),
            f"{estimated} of {len(samples)} cycles ran back to back inside a longer busy "
            "interval; the trace has no event at their boundaries, so their split is estimated "
            "from the median CPU time per cycle." if estimated else
            "Every cycle ended with a sleep, so no boundary was estimated.",
            f"The trace covers {tr.duration_ms:.0f} ms; cycles cut off at either end are left out.",
        ],
    }


def periodic_threads(tr: kev.Trace, keys=None, limit: int = 8) -> list[dict]:
    """Every periodic thread in the trace (or among `keys`), busiest first."""
    from .traceview import KERNEL_PID, _is_tracer
    keys = keys if keys is not None else [k for k in tr.spans
                                          if k[0] != KERNEL_PID and not _is_tracer(tr, k)]
    out = []
    for k in keys:
        c = cycles(tr, k)
        if c:
            out.append(c)
    out.sort(key=lambda c: (-c["report"]["misses"], c["period_ms"]))
    return out[:limit]
