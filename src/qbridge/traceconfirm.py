"""Check a diagnosis finding against a kernel trace from the same run.

A finding from analysis.py is built from thread snapshots: a handful of
moments. A kernel trace (parsers/kev.py) records every thread state change
for a short window, so it can show whether the story in the finding actually
happens, step by step, and for how long.

Each finding is broken into steps. A step is "supported" only if trace events
show it; the finding is marked confirmed only if every step is supported.
Otherwise the original confidence stays and the unsupported steps are listed
under `not_shown`. The trace is short (about a second), so a pattern that
did not occur inside that window is reported as not shown, not as disproved.

What a trace cannot show is said too: QNX's THMUTEX event names the waiting
thread but not the mutex or its owner, so the holder's identity still comes
from the snapshot (the mutex owner word); the trace adds what the holder and
the other threads were doing while the waiter was blocked.
"""

from __future__ import annotations

from .analysis import (RULE_CPU_PREEMPTION, RULE_LATE_WAKEUP, RULE_MSGPASS_INVERSION,
                       RULE_MUTEX_INVERSION, RULE_OVERRUN)
from .parsers import kev

KERNEL_PID = 1
SHARE = 0.5          # "most of the wait": at least half of the waiting time
WAKE_SLACK_MS = 0.05  # holder still running when the waiter is woken (event order)


def _pct(part: float, whole: float) -> str:
    return f"{100.0 * part / whole:.0f}%" if whole > 0 else "n/a"


def _spans(tr: kev.Trace, key, state: str) -> list[kev.Span]:
    return [s for s in tr.spans.get(key, []) if s.state == state]


def _step(claim: str, ok: bool, detail: str) -> dict:
    return {"claim": claim, "supported": bool(ok), "detail": detail}


def _timeline(tr: kev.Trace, lanes: list[tuple[str, tuple[int, int]]], around: kev.Span | None,
              margin_ms: float = 6.0) -> dict | None:
    """Spans of the given threads in a window around one wait, for the report."""
    if around is None:
        return None
    start, end = max(0.0, around.start - margin_ms), min(tr.duration_ms, around.end + margin_ms)
    out = []
    for name, key in lanes:
        segs = [{"state": s.state, "start": round(max(s.start, start) - start, 3),
                 "end": round(min(s.end, end) - start, 3), "priority": s.priority, "cpu": s.cpu}
                for s in tr.spans.get(key, []) if s.end > start and s.start < end]
        out.append({"lane": name, "thread": tr.label(key), "spans": segs})
    return {"window_start_ms": round(start, 3), "window_ms": round(end - start, 3), "lanes": out}


def _key(tr: kev.Trace, pid: int, tid: int, name: str | None) -> tuple[tuple[int, int] | None, str]:
    """The trace thread for a snapshot thread, checking the name when the trace has one."""
    key = (int(pid), int(tid))
    if key in tr.spans:
        tname = tr.thread_names.get(key)
        if name and tname and tname != name:
            return None, f"pid {pid} tid {tid} is '{tname}' in the trace, not '{name}'"
        return key, ""
    by_name = tr.find_thread(name, int(pid)) if name else None
    if by_name and by_name in tr.spans:
        return by_name, ""
    return None, f"no events for pid {pid} tid {tid} ('{name}') in the trace"


def confirm_preemption(f: dict, tr: kev.Trace) -> dict:
    sub = f["subject"]
    task, runner = sub["task"], sub["runner"]
    tk, why_t = _key(tr, sub["pid"], task["tid"], task.get("thread_name"))
    rk, why_r = _key(tr, runner["pid"], runner["tid"], runner.get("thread_name"))
    steps, longest = [], None
    if not tk or not rk:
        steps.append(_step("The finding's threads appear in the trace", False, why_t or why_r))
        return {"steps": steps, "timeline": None}

    ready = _spans(tr, tk, "READY")
    ready_ms = sum(s.ms for s in ready)
    mutex_ms = sum(s.ms for s in _spans(tr, tk, "MUTEX"))
    steps.append(_step(
        f"{tr.label(tk)} is ready to run but not running, and not waiting on a lock",
        ready_ms >= 1.0 and mutex_ms <= 0.05 * max(ready_ms, 1.0),
        f"READY {ready_ms:.1f} ms in {len(ready)} span(s); MUTEX {mutex_ms:.1f} ms "
        f"in a {tr.duration_ms:.0f} ms trace"))

    on_cpu = above = 0.0
    for s in ready:
        for (k, p), ms in tr.running_during(s.cpu, s.start, s.end, exclude=tk).items():
            if k == rk:
                on_cpu += ms
                if p > s.priority:
                    above += ms
    steps.append(_step(
        f"While it waits, {tr.label(rk)} runs on the same CPU",
        ready_ms > 0 and on_cpu >= SHARE * ready_ms,
        f"{tr.label(rk)} was RUNNING on the waiting thread's CPU for {on_cpu:.1f} ms of its "
        f"{ready_ms:.1f} ms READY time ({_pct(on_cpu, ready_ms)})"))
    steps.append(_step(
        f"{tr.label(rk)} runs at a higher priority than the waiting thread",
        on_cpu > 0 and above >= 0.9 * on_cpu,
        f"{above:.1f} of those {on_cpu:.1f} ms were at a priority above the waiting thread's "
        f"(priorities as recorded in the THREAD events)"))
    if ready:
        longest = max(ready, key=lambda s: s.ms)
        top = sorted(tr.running_during(longest.cpu, longest.start, longest.end, exclude=tk).items(),
                     key=lambda kv: -kv[1])[:2]
        steps[-1]["longest_wait"] = {
            "ms": round(longest.ms, 2), "start_ms": round(longest.start, 2), "cpu": longest.cpu,
            "ran_instead": [{"thread": tr.label(k), "priority": p, "ms": round(ms, 2)}
                            for (k, p), ms in top]}
    lanes = [("task", tk), ("runner", rk)]
    return {"steps": steps, "timeline": _timeline(tr, lanes, longest)}


def confirm_inversion(f: dict, tr: kev.Trace) -> dict:
    sub = f["subject"]
    w, h = sub["waiter"], sub["holder"]
    wk, why_w = _key(tr, sub["pid"], w["tid"], w.get("thread_name"))
    hk, why_h = _key(tr, sub["pid"], h["tid"], h.get("thread_name"))
    steps, longest = [], None
    if not wk or not hk:
        steps.append(_step("The finding's threads appear in the trace", False, why_w or why_h))
        return {"steps": steps, "timeline": None}

    waits = _spans(tr, wk, "MUTEX")
    wait_ms = sum(s.ms for s in waits)
    steps.append(_step(
        f"{tr.label(wk)} blocks on a mutex",
        wait_ms >= 1.0,
        f"MUTEX {wait_ms:.1f} ms in {len(waits)} span(s) in a {tr.duration_ms:.0f} ms trace"))

    # What the holder did during the waits, and at which priority.
    h_ready = h_ready_low = h_run = 0.0
    middle = 0.0
    middle_names: dict = {}
    for s in waits:
        for (st, p), ms in tr.states_during(hk, s.start, s.end).items():
            if st == "READY":
                h_ready += ms
                if p < w["priority"]:
                    h_ready_low += ms
            elif st == "RUNNING":
                h_run += ms
        for hs in _spans(tr, hk, "READY"):
            a, b = max(hs.start, s.start), min(hs.end, s.end)
            if b <= a:
                continue
            for (k, p), ms in tr.running_during(hs.cpu, a, b, exclude=hk).items():
                if k != wk and k[0] != KERNEL_PID and hs.priority < p < w["priority"]:
                    middle += ms
                    middle_names[(tr.label(k), p)] = middle_names.get((tr.label(k), p), 0.0) + ms
    top = sorted(middle_names.items(), key=lambda kv: -kv[1])[:3]
    steps.append(_step(
        f"Meanwhile the holder {tr.label(hk)} is ready but not running, below the waiter's "
        f"priority (no inheritance)",
        wait_ms > 0 and h_ready_low >= SHARE * wait_ms,
        f"holder READY {h_ready:.1f} ms of the {wait_ms:.1f} ms wait ({_pct(h_ready, wait_ms)}), "
        f"{h_ready_low:.1f} ms of it below priority {w['priority']}; RUNNING {h_run:.1f} ms"))
    steps.append(_step(
        "A middle-priority thread holds the holder's CPU",
        h_ready > 0 and middle >= SHARE * h_ready,
        (f"{', '.join(f'{n} (priority {p}, {ms:.1f} ms)' for (n, p), ms in top)}"
         + (" and others" if len(middle_names) > 3 else "") + " ran "
         if middle_names else "No thread between the two priorities ran ")
        + f"for {middle:.1f} ms of the holder's {h_ready:.1f} ms READY time "
          f"({_pct(middle, h_ready)})"))

    # Each wait should end while the holder is running (it unlocked the mutex).
    h_running = _spans(tr, hk, "RUNNING")
    ended = [s for s in waits if s.end < tr.duration_ms - 0.01]       # not cut off by the trace end
    woken = sum(1 for s in ended
                if any(r.start - WAKE_SLACK_MS <= s.end <= r.end + WAKE_SLACK_MS for r in h_running))
    steps.append(_step(
        "Each wait ends while the holder is running (it releases the mutex)",
        bool(ended) and woken == len(ended),
        f"{woken} of {len(ended)} completed waits ended while the holder was RUNNING"))
    if waits:
        longest = max(waits, key=lambda s: s.ms)
    lanes = [("waiter", wk), ("holder", hk)]
    for m in sub.get("middle", [])[:2]:
        mk, _ = _key(tr, m["pid"], m["tid"], m.get("thread_name"))
        if mk:
            lanes.append(("middle", mk))
    return {"steps": steps, "timeline": _timeline(tr, lanes, longest)}


def confirm_msgpass(f: dict, tr: kev.Trace) -> dict:
    sub = f["subject"]
    c, srv = sub["client"], sub["server"]
    ck, why_c = _key(tr, sub["pid"], c["tid"], c.get("thread_name"))
    sk, why_s = _key(tr, srv["pid"], srv["tid"], srv.get("thread_name"))
    steps, longest = [], None
    if not ck or not sk:
        steps.append(_step("The finding's threads appear in the trace", False, why_c or why_s))
        return {"steps": steps, "timeline": None}

    waits = [s for s in tr.spans.get(ck, []) if s.state in ("SEND", "REPLY")]
    wait_ms = sum(s.ms for s in waits)
    reply_ms = sum(s.ms for s in waits if s.state == "REPLY")
    steps.append(_step(
        f"{tr.label(ck)} blocks waiting for a server (SEND or REPLY)",
        wait_ms >= 1.0,
        f"SEND/REPLY {wait_ms:.1f} ms ({reply_ms:.1f} ms of it REPLY, the server holding its "
        f"message) in {len(waits)} span(s) in a {tr.duration_ms:.0f} ms trace"))

    s_ready = s_ready_low = s_run = 0.0
    middle = 0.0
    middle_names: dict = {}
    for s in waits:
        for (st, p), ms in tr.states_during(sk, s.start, s.end).items():
            if st == "READY":
                s_ready += ms
                if p < c["priority"]:
                    s_ready_low += ms
            elif st == "RUNNING":
                s_run += ms
        for ss in _spans(tr, sk, "READY"):
            a, b = max(ss.start, s.start), min(ss.end, s.end)
            if b <= a:
                continue
            for (k, p), ms in tr.running_during(ss.cpu, a, b, exclude=sk).items():
                if k != ck and k[0] != KERNEL_PID and ss.priority < p < c["priority"]:
                    middle += ms
                    middle_names[(tr.label(k), p)] = middle_names.get((tr.label(k), p), 0.0) + ms
    top = sorted(middle_names.items(), key=lambda kv: -kv[1])[:3]
    # While the server is RUNNING it is doing the client's work, which is a
    # normal part of the wait. The question is what the rest of the wait was.
    held_off = wait_ms - s_run
    steps.append(_step(
        f"Meanwhile the server {tr.label(sk)} is ready but not running, below the client's "
        f"priority (no inheritance)",
        held_off >= 1.0 and s_ready_low >= SHARE * held_off,
        f"of the {wait_ms:.1f} ms wait the server was RUNNING (doing the work) for {s_run:.1f} ms; "
        f"of the other {held_off:.1f} ms it was READY for {s_ready:.1f} ms "
        f"({_pct(s_ready, held_off)}), {s_ready_low:.1f} ms of it below priority {c['priority']}"))
    steps.append(_step(
        "A middle-priority thread holds the server's CPU",
        s_ready > 0 and middle >= SHARE * s_ready,
        (f"{', '.join(f'{n} (priority {p}, {ms:.1f} ms)' for (n, p), ms in top)}"
         + (" and others" if len(middle_names) > 3 else "") + " ran "
         if middle_names else "No thread between the two priorities ran ")
        + f"for {middle:.1f} ms of the server's {s_ready:.1f} ms READY time "
          f"({_pct(middle, s_ready)})"))

    s_running = _spans(tr, sk, "RUNNING")
    replies = [s for s in waits if s.state == "REPLY" and s.end < tr.duration_ms - 0.01]
    woken = sum(1 for s in replies
                if any(r.start - WAKE_SLACK_MS <= s.end <= r.end + WAKE_SLACK_MS for r in s_running))
    steps.append(_step(
        "Each wait for a reply ends while the server is running (it replies)",
        bool(replies) and woken == len(replies),
        f"{woken} of {len(replies)} completed REPLY waits ended while the server was RUNNING"))
    if waits:
        longest = max(waits, key=lambda s: s.ms)
    lanes = [("client", ck), ("server", sk)]
    for m in sub.get("middle", [])[:2]:
        mk, _ = _key(tr, m["pid"], m["tid"], m.get("thread_name"))
        if mk:
            lanes.append(("middle", mk))
    return {"steps": steps, "timeline": _timeline(tr, lanes, longest)}


def _task_key(tr: kev.Trace, sub: dict) -> tuple[tuple[int, int] | None, str]:
    """The finding's task in the trace: by pid and tid when known, else by thread name."""
    task = sub.get("task") or {}
    name = task.get("thread_name")
    if sub.get("pid") is not None and task.get("tid") is not None:
        return _key(tr, sub["pid"], task["tid"], name)
    k = tr.find_thread(name) if name else None
    return (k, "") if k and k in tr.spans else (None, f"no thread named '{name}' in the trace")


def _rebuilt(tr: kev.Trace, tk) -> tuple[dict | None, dict]:
    from . import tracecycles
    c = tracecycles.cycles(tr, tk, keep_samples=True)
    if not c:
        return None, _step("The task's cycles can be rebuilt from the trace", False,
                           f"{tr.label(tk)} does not wake up on a regular period in this "
                           f"{tr.duration_ms:.0f} ms trace")
    return c, _step("The task's cycles can be rebuilt from the trace", True,
                    f"{c['cycles']} cycles of {tr.label(tk)} on a {c['period_ms']:.2f} ms period "
                    f"({c['estimated_cycles']} with estimated boundaries)")


def confirm_late_wakeup(f: dict, tr: kev.Trace) -> dict:
    tk, why = _task_key(tr, f["subject"])
    if not tk:
        return {"steps": [_step("The finding's task appears in the trace", False, why)], "timeline": None}
    c, first = _rebuilt(tr, tk)
    steps = [first]
    if not c:
        return {"steps": steps, "timeline": None}
    samples = c["samples"]
    lat = sorted(s.latency for s in samples)
    typical = lat[len(lat) // 2] if lat else 0.0
    # Late enough to matter for a deadline: a quarter of the period at least.
    thr = max(1000.0, 3 * typical, 0.25 * c["period_ms"] * 1000)
    late = [s for s in samples if s.latency > thr]
    steps.append(_step(
        "Some cycles in the trace start late, by at least a quarter of the period", bool(late),
        f"{len(late)} of {len(samples)} cycles started more than {thr / 1000:.1f} ms after their "
        f"release"
        + (f", the latest {max(s.latency for s in late) / 1000:.1f} ms" if late else "")
        + f" (usual {typical / 1000:.2f} ms)"))
    late_ms = above_ms = kernel_ms = asleep_ms = 0.0
    spans = tr.spans.get(tk, [])
    from .tracecycles import IDLE
    worst = None
    for s in late:
        a, b = s.release / 1000, (s.release + s.latency) / 1000
        late_ms += b - a
        prio = next((x.priority for x in spans if x.state == "RUNNING" and x.end > b), None)
        cpus = {x.cpu for x in spans if x.end > a and x.start < b} or {x.cpu for x in spans[:1]}
        for cpu in cpus:
            for (k, p), ms in tr.running_during(cpu, a, b, exclude=tk).items():
                if prio is not None and p > prio:
                    if k[0] == KERNEL_PID:
                        kernel_ms += ms
                    else:
                        above_ms += ms
        asleep_ms += sum(ms for (st, _), ms in tr.states_during(tk, a, b).items() if st in IDLE)
        if worst is None or s.latency > worst.latency:
            worst = s
    if late:
        steps.append(_step(
            "While it waited to start, no higher-priority thread ran on its CPU",
            above_ms <= 0.1 * late_ms,
            f"threads above its priority ran for {above_ms:.1f} ms of the {late_ms:.1f} ms of late "
            f"starts; kernel threads (interrupts, clock) for {kernel_ms:.1f} ms"))
        steps.append(_step(
            "The delay was in the wake-up itself: it was still asleep after its release",
            asleep_ms >= SHARE * late_ms,
            f"asleep for {asleep_ms:.1f} ms of the {late_ms:.1f} ms ({_pct(asleep_ms, late_ms)}); "
            "the rest it was ready but not yet running"))
    around = None
    if worst is not None:
        a = worst.release / 1000
        around = kev.Span("LATE", a, a + worst.latency / 1000, 0, 0)
    return {"steps": steps, "timeline": _timeline(tr, [("task", tk)], around)}


def confirm_overrun(f: dict, tr: kev.Trace) -> dict:
    tk, why = _task_key(tr, f["subject"])
    if not tk:
        return {"steps": [_step("The finding's task appears in the trace", False, why)], "timeline": None}
    c, first = _rebuilt(tr, tk)
    steps = [first]
    if not c:
        return {"steps": steps, "timeline": None}
    samples = c["samples"]
    ex = sorted(s.exec for s in samples)
    typical = ex[len(ex) // 2] if ex else 0.0
    thr = max(1000.0, 3 * typical, 0.25 * c["period_ms"] * 1000)
    long_ = [s for s in samples if s.exec > thr]
    steps.append(_step(
        "Some cycles in the trace use far more CPU time than usual", bool(long_),
        f"{len(long_)} of {len(samples)} cycles used more than {thr / 1000:.1f} ms "
        f"of CPU time" + (f", the most {max(s.exec for s in long_) / 1000:.1f} ms" if long_ else "")
        + f" (median {typical / 1000:.2f} ms)"))
    if long_:
        waits = sum(s.lock_wait + s.server_wait for s in long_)
        cpu = sum(s.exec for s in long_)
        steps.append(_step(
            "In those cycles it was not waiting for a lock or a server",
            waits <= 0.1 * cpu,
            f"lock and server waits {waits / 1000:.1f} ms against {cpu / 1000:.1f} ms of CPU time"))
    worst = max(long_, key=lambda s: s.exec) if long_ else None
    around = (kev.Span("LONG", worst.release / 1000, (worst.release + worst.response) / 1000, 0, 0)
              if worst else None)
    return {"steps": steps, "timeline": _timeline(tr, [("task", tk)], around)}


NOT_IN_TRACE = {
    RULE_LATE_WAKEUP: ("The trace shows when the task woke and what ran, not why a timer fired "
                       "late: a late timer and a virtual machine pausing the guest look the same."),
    RULE_OVERRUN: ("The trace counts the time the task was RUNNING; a virtual machine that paused "
                   "the guest during that time is counted too."),
    RULE_MSGPASS_INVERSION: ("The trace does not name the server a client waits on (THSEND and "
                             "THREPLY only name the waiting thread, and message events are not "
                             "recorded); the server's identity comes from the snapshot."),
    RULE_MUTEX_INVERSION: ("The trace does not name the mutex or its owner (THMUTEX only names "
                           "the waiting thread); the holder's identity comes from the snapshot's "
                           "mutex owner word."),
    RULE_CPU_PREEMPTION: None,
}


def confirm(finding: dict, tr: kev.Trace, trace_id: str | None = None) -> dict:
    """Check one finding; returns the trace check and sets finding['trace']."""
    rule = finding.get("rule")
    if rule == RULE_CPU_PREEMPTION:
        r = confirm_preemption(finding, tr)
    elif rule == RULE_MUTEX_INVERSION:
        r = confirm_inversion(finding, tr)
    elif rule == RULE_MSGPASS_INVERSION:
        r = confirm_msgpass(finding, tr)
    elif rule == RULE_LATE_WAKEUP:
        r = confirm_late_wakeup(finding, tr)
    elif rule == RULE_OVERRUN:
        r = confirm_overrun(finding, tr)
    else:
        return {"status": "not checked", "reason": f"no trace check for rule {rule!r}"}
    steps = r["steps"]
    ok = bool(steps) and all(s["supported"] for s in steps)
    out = {"trace_id": trace_id, "status": "confirmed" if ok else "not confirmed",
           "trace_ms": round(tr.duration_ms, 1), "steps": steps,
           "not_shown": [s["claim"] for s in steps if not s["supported"]],
           "limits": [x for x in (NOT_IN_TRACE.get(rule),
                                  f"The trace covers {tr.duration_ms:.0f} ms of the run; it shows "
                                  "what happened in that window only.") if x],
           "timeline": r["timeline"]}
    finding["trace"] = out
    finding["confirmed_by_trace"] = ok
    return out


def confirm_all(result: dict, tr: kev.Trace, trace_id: str | None = None) -> dict:
    """Check every finding of a diagnosis and extend its summary."""
    every = [confirm(f, tr, trace_id) for f in result.get("findings", [])]
    checked = [c for c in every if c.get("status") != "not checked"]
    skipped = len(every) - len(checked)
    n_ok = sum(1 for c in checked if c.get("status") == "confirmed")
    if checked:
        result["summary"] += (f" Kernel trace ({tr.duration_ms:.0f} ms): {n_ok} of "
                              f"{len(checked)} finding(s) confirmed step by step"
                              + (f"; {skipped} not checked (no trace check for that rule yet)"
                                 if skipped else "") + ".")
    elif every:
        result["summary"] += (f" A kernel trace ({tr.duration_ms:.0f} ms) was captured; the "
                              "findings' rules have no trace check yet.")
    else:
        result["summary"] += (f" A kernel trace ({tr.duration_ms:.0f} ms) was captured; there "
                              "was no finding to check against it.")
    result["trace_id"] = trace_id
    return result
