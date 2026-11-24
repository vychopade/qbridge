"""Who waited for whom: blocked intervals tied to the thread that ended them, from
kernel-call and communication events in a trace, across processes and CPUs.

A THREAD-class event says a thread entered MUTEX, SEND, REPLY, CONDVAR or SEM; it
does not say what it waits for. When the trace also has the kernel call and
communication classes (tracelogger without -F1/-F6), the decoded events do:

  mutex     the waiter's SyncMutexLock (sync_p, owner) just before it blocks, and
            the SyncMutexUnlock of the same sync_p by another thread that ends the
            wait. With wide events, the owner value in the waiter's call equals
            the unlocker's own owner value (less the waiting bit): the trace
            itself names the holder, without decoding what the owner value means.
  message   the client's COMM SND_MESSAGE (rcvid, server pid), the server
            thread's REC_MESSAGE with the same rcvid, and REPLY_MESSAGE /
            MSG_ERROR naming the client (tid, pid), logged by the replying thread.
  condvar   the waiter's SyncCondvarWait (sync_p) and the SyncCondvarSignal of
  semaphore the same object by the thread that woke it (SyncSemWait / SyncSemPost).

Each kernel call is attributed to the thread RUNNING on that CPU when it was
logged (the caller); see kev.build().

With the holder known, the interval is checked: what the holder did meanwhile
(running, ready, itself blocked: a chain), at which priority (inheritance shows
as the holder running at the waiter's priority), and who ran on the holder's CPU
while it was ready, by priority band. Classification:

  confirmed_priority_inversion      the holder is proven by trace events, it ran
      below the waiter's priority, and threads between the two priorities ran on
      its CPU while it was ready, for a significant part of the wait.
  probable_scheduling_interference  the delay is explained by threads holding
      the CPU, but the dependency is not proven (no kernel-call/communication
      events: a candidate from thread states only), or the holder was held off
      by threads at or above the waiter's priority (interference, not an
      inversion), or events were dropped during the interval.
  no_inversion                      the holder is proven and ran promptly, or at
      the waiter's priority (inheritance): the wait is the holder's own work.
  insufficient_evidence             the trace does not show who ended the wait.

Nothing is attributed without an event that names it; every finding lists the
events (time, CPU, thread, fields, file offset) it rests on.
"""

from __future__ import annotations

import bisect
from collections import defaultdict

from .parsers import kev

KERNEL_PID = 1
KINDS = {"MUTEX": "mutex", "SEND": "message", "REPLY": "message", "CONDVAR": "condvar",
         "SEM": "semaphore"}
MERGE_GAP_MS = 0.2          # a wait split by a moment of RUNNING is one wait
MIN_WAIT_MS = 0.5           # shorter waits are not analysed
SIGNIFICANT = 0.1           # "a significant part of the wait": a tenth, and at least
MIN_EFFECT_MS = 1.0         # one millisecond
WAITING_BIT = 0x80000000
LOOKBACK_MS = 1.0           # the blocking call is logged just before the state change
DROP_REACH_MS = 10.0        # a drop logged this soon after an interval may hide part of it


def _owner(v) -> int | None:
    """A mutex owner word as an unsigned 32-bit value without the waiting bit."""
    n = _i(v)
    return None if n is None else (n & 0xFFFFFFFF) & ~WAITING_BIT


def _i(v, default=None):
    try:
        return int(str(v), 0)
    except (TypeError, ValueError):
        return default


class _Index:
    """Link events by kind, sorted by time, and RUNNING spans per CPU."""

    def __init__(self, tr: kev.Trace):
        self.tr = tr
        self.by_ev: dict = defaultdict(list)
        for l in tr.links:
            self.by_ev[(l["cls"], l["ev"])].append(l)
        for v in self.by_ev.values():
            v.sort(key=lambda l: l["t"])
        self.times = {k: [l["t"] for l in v] for k, v in self.by_ev.items()}
        self.run: dict = defaultdict(list)            # cpu -> [(start, end, key, prio)]
        for k, spans in tr.spans.items():
            for s in spans:
                if s.state == "RUNNING":
                    self.run[s.cpu].append((s.start, s.end, k, s.priority))
        for v in self.run.values():
            v.sort()

    def events(self, cls: str, ev: str, a: float, b: float):
        k = (cls, ev)
        ts = self.times.get(k)
        if not ts:
            return []
        return self.by_ev[k][bisect.bisect_left(ts, a):bisect.bisect_right(ts, b)]

    def ran(self, cpu: int, a: float, b: float) -> dict:
        out: dict = defaultdict(float)
        for s, e, k, p in self.run.get(cpu, []):
            if s >= b:
                break
            if e > a:
                out[(k, p)] += min(b, e) - max(a, s)
        return out


def _ref(tr: kev.Trace, l: dict, role: str) -> dict:
    """One piece of evidence, as it appears in the trace."""
    r = {"role": role, "t_ms": round(l["t"], 4), "cpu": l["cpu"], "event": f"{l['cls']}:{l['ev']}",
         "thread": tr.label(l["by"]) if l.get("by") else None, "fields": l["f"]}
    if l.get("off") is not None:
        r["file_offset"] = l["off"]
    return r


def waits(tr: kev.Trace, key, min_ms: float = MIN_WAIT_MS) -> list[dict]:
    """The thread's blocked intervals (MUTEX, SEND+REPLY, CONDVAR, SEM), merged across
    short interruptions, as {kind, start, end, states, priority, cpu}."""
    out: list[dict] = []
    for s in tr.spans.get(key, []):
        kind = KINDS.get(s.state)
        if kind is None:
            if s.state == "RUNNING" and out and out[-1].get("_open") and s.ms <= MERGE_GAP_MS:
                out[-1]["_gap"] = True
                continue
            if out:
                out[-1]["_open"] = False
            continue
        last = out[-1] if out else None
        if (last and last.get("_open") and last["kind"] == kind
                and s.start - last["end"] <= MERGE_GAP_MS):
            last["end"] = s.end
            last["states"].append(s.state)
            continue
        out.append({"kind": kind, "start": s.start, "end": s.end, "states": [s.state],
                    "priority": s.priority, "cpu": s.cpu, "_open": True})
    return [{k: v for k, v in w.items() if not k.startswith("_")} for w in out
            if w["end"] - w["start"] >= min_ms]


# -- who ended the wait ---------------------------------------------------------------------

def _mutex(ix: _Index, key, w: dict) -> dict:
    tr = ix.tr
    a, b = w["start"], w["end"]
    calls = [l for l in ix.events("KER_CALL", "SYNC_MUTEX_LOCK", a - LOOKBACK_MS, b)
             if l["by"] == key]
    if not calls:
        return {"why": "no SyncMutexLock by the waiter in the trace (kernel-call class off?)"}
    lock = calls[-1]
    sync = lock["f"].get("sync_p")
    owner = _owner(lock["f"].get("owner"))
    ev = [_ref(tr, lock, "waiter's SyncMutexLock")]
    unl = [l for l in ix.events("KER_CALL", "SYNC_MUTEX_UNLOCK", a, b + 0.05)
           if l["f"].get("sync_p") == sync and l["by"] and l["by"] != key]
    if unl:
        u = unl[-1]
        ev.append(_ref(tr, u, "holder's SyncMutexUnlock of the same mutex"))
        uo = _owner(u["f"].get("owner"))
        proof = "released the mutex (SyncMutexUnlock, same sync_p) when the wait ended"
        if owner is not None and uo is not None:
            if owner == uo:
                proof += f"; the owner value in the waiter's call equals the unlocker's (0x{owner:08x})"
            else:
                proof += "; the owner value differs from the waiter's call (ownership changed)"
        return {"holder": u["by"], "object": sync, "proof": proof, "evidence": ev}
    if owner:
        same = [l for l in ix.by_ev.get(("KER_CALL", "SYNC_MUTEX_UNLOCK"), [])
                if l["by"] and l["by"] != key
                and _owner(l["f"].get("owner")) == owner]
        if same:
            ev.append(_ref(tr, same[0], "the same owner value in this thread's own unlock"))
            return {"holder": same[0]["by"], "object": sync, "evidence": ev,
                    "proof": (f"the waiter's SyncMutexLock names owner 0x{owner:08x}, "
                              "the owner value this thread uses in its own SyncMutexUnlock "
                              "elsewhere in the trace (the release itself was not logged)")}
    return {"object": sync, "evidence": ev,
            "why": "the release of this mutex is not in the trace, and its owner value is not "
                   "seen in any thread's unlock"}


def _message(ix: _Index, key, w: dict) -> dict:
    tr = ix.tr
    a, b = w["start"], w["end"]
    snd = [l for l in ix.events("COMM", "SND_MESSAGE", a - LOOKBACK_MS, b) if l["by"] == key]
    pid, tid = key
    ends = [l for l in ix.events("COMM", "REPLY_MESSAGE", a, b + 0.05)
            + ix.events("COMM", "MSG_ERROR", a, b + 0.05)
            if _i(l["f"].get("pid")) == pid and _i(l["f"].get("tid")) == tid and l["by"]]
    ev = []
    rcvid = server = None
    if snd:
        s = snd[-1]
        rcvid, server = s["f"].get("rcvid"), _i(s["f"].get("pid"))
        ev.append(_ref(tr, s, "client's message send (COMM SND_MESSAGE)"))
        rec = [l for l in ix.events("COMM", "REC_MESSAGE", s["t"], b + 0.05)
               if l["f"].get("rcvid") == rcvid and l["by"]]
        if rec:
            ev.append(_ref(tr, rec[0], "server thread receives it (same rcvid)"))
    if ends:
        e = ends[0]
        ev.append(_ref(tr, e, "server thread replies to this client"))
        return {"holder": e["by"], "object": rcvid, "evidence": ev,
                "receiver": next((r["thread"] for r in ev if r["role"].startswith("server thread rec")), None),
                "proof": (f"{tr.label(e['by'])} replied to this thread ({e['ev']} naming tid {tid}, "
                          f"pid {pid}) when the wait ended")
                         + (f"; the message went to process {server} (rcvid {rcvid})" if rcvid else "")}
    if snd and len(ev) > 1:
        r = next(l for l in ix.events("COMM", "REC_MESSAGE", snd[-1]["t"], b + 0.05)
                 if l["f"].get("rcvid") == rcvid and l["by"])
        return {"holder": r["by"], "object": rcvid, "evidence": ev,
                "proof": f"{tr.label(r['by'])} received this thread's message (rcvid {rcvid}); "
                         "the reply is past the end of the trace or not logged"}
    return {"evidence": ev, "why": "no send/reply events for this thread in the trace "
                                   "(communication class off?)" if not ev else
            "the message was sent but no server thread is seen receiving or replying"}


def _wakeup(ix: _Index, key, w: dict, wait_call: str, wake_call: str, what: str) -> dict:
    tr = ix.tr
    a, b = w["start"], w["end"]
    calls = [l for l in ix.events("KER_CALL", wait_call, a - LOOKBACK_MS, b) if l["by"] == key]
    if not calls:
        return {"why": f"no {wait_call} by the waiter in the trace (kernel-call class off?)"}
    c = calls[-1]
    sync = c["f"].get("sync_p")
    ev = [_ref(tr, c, f"waiter's {wait_call}")]
    wakes = [l for l in ix.events("KER_CALL", wake_call, a, b + 0.05)
             if l["f"].get("sync_p") == sync and l["by"] and l["by"] != key]
    if not wakes:
        return {"object": sync, "evidence": ev,
                "why": f"no {wake_call} of this {what} by another thread during the wait"}
    wk = wakes[-1]
    ev.append(_ref(tr, wk, f"{wake_call} of the same {what} that ended the wait"))
    return {"holder": wk["by"], "object": sync, "evidence": ev,
            "proof": f"{tr.label(wk['by'])} ended the wait with {wake_call} on the same {what} "
                     f"(sync_p {sync}); a {what} has no owner, so this is the thread it waited for "
                     "in this instance, as observed"}


def _holder(ix: _Index, key, w: dict) -> dict:
    if w["kind"] == "mutex":
        return _mutex(ix, key, w)
    if w["kind"] == "message":
        return _message(ix, key, w)
    if w["kind"] == "condvar":
        return _wakeup(ix, key, w, "SYNC_CONDVAR_WAIT", "SYNC_CONDVAR_SIGNAL", "condition variable")
    return _wakeup(ix, key, w, "SYNC_SEM_WAIT", "SYNC_SEM_POST", "semaphore")


# -- what happened meanwhile ------------------------------------------------------------------

def _base_priority(tr: kev.Trace, key, at: float) -> int | None:
    """The thread's priority just before `at` (its own, before any boost during the wait)."""
    spans = [s for s in tr.spans.get(key, []) if s.priority >= 0]
    before = [s for s in spans if s.end <= at + 1e-9]
    if before:
        return before[-1].priority
    covering = [s for s in spans if s.start <= at < s.end]  # e.g. in its first state
    return covering[0].priority if covering else None


def _dropped(tr: kev.Trace, a: float, b: float, cpus: set) -> list[dict]:
    """CONTROL DROPPED is logged when tracing resumes, so events before it, back to the
    previous buffer, may be missing: a drop logged during [a, b] (b is extended by
    DROP_REACH_MS by the callers) may hide part of that interval."""
    return [d for d in tr.drops if a <= d["t"] <= b and d["cpu"] in cpus]


def analyse_wait(tr: kev.Trace, key, w: dict, ix: _Index | None = None, depth: int = 0,
                 root_priority: int | None = None) -> dict:
    """One wait. In a chain (the holder itself blocked), root_priority is the priority of
    the thread at the start of the chain: that is the priority the bands are judged by."""
    ix = ix or _Index(tr)
    a, b = w["start"], w["end"]
    wait_ms = b - a
    pw = w["priority"]
    if root_priority is not None and root_priority > pw:
        pw = root_priority
    h = _holder(ix, key, w)
    out = {"kind": w["kind"], "waiter": tr.label(key), "pid": key[0], "tid": key[1],
           "waiter_priority": w["priority"], "waiter_cpu": w["cpu"], "start_ms": round(a, 4),
           "end_ms": round(b, 4), "waited_ms": round(wait_ms, 3), "states": sorted(set(w["states"])),
           "object": h.get("object"), "evidence": h.get("evidence", [])}
    if pw != w["priority"]:
        out["judged_at_priority"] = pw            # the chain's first waiter

    # Every CPU: who ran during the wait (top three each).
    cpus = sorted(ix.run)
    out["cpus_during_wait"] = {
        str(c): [{"thread": tr.label(k), "priority": p, "ms": round(ms, 3)}
                 for (k, p), ms in sorted(ix.ran(c, a, b).items(), key=lambda kv: -kv[1])[:3]]
        for c in cpus}
    holder = h.get("holder")
    first = min((s.start for s in tr.spans.get(key, [])), default=a)
    if b >= tr.duration_ms - 1e-6:
        out["cut_by_trace"] = "end"              # still waiting when the trace stopped
    elif a <= first + 1e-6:
        out["cut_by_trace"] = "start"            # already waiting when it started
    if not holder:
        why = h.get("why", "the trace does not show who ended the wait")
        if out.get("cut_by_trace") == "end":
            why = "the thread was still waiting when the trace ended, so its end is not recorded"
        elif out.get("cut_by_trace") == "start":
            why = "the thread was already waiting when the trace started, so its start is not recorded"
        # The holder is unknown, so a drop on any CPU may have hidden it.
        drops = _dropped(tr, a, b + DROP_REACH_MS, {d["cpu"] for d in tr.drops})
        if drops:
            why += f" (the kernel dropped {sum(d['count'] for d in drops)} events around this wait)"
            out["dropped_events"] = [{"t_ms": round(d["t"], 3), "cpu": d["cpu"], "count": d["count"]}
                                     for d in drops]
        out.update(classification="insufficient_evidence", dependency="unknown", reason=why)
        out["text"] = (f"{tr.label(key)} waited {wait_ms:.2f} ms ({w['kind']}); the trace does not "
                       f"show who it waited for: {out['reason']}.")
        return out
    out["dependency"] = "proven by trace events"
    out["proof"] = h["proof"]
    if h.get("receiver"):
        out["receiver"] = h["receiver"]
    states = tr.states_during(holder, a, b)
    hs: dict = defaultdict(float)
    prios: set = set()
    for (st, p), ms in states.items():
        hs[st] += ms
        if st in ("RUNNING", "READY") and p >= 0:
            prios.add(p)
    base = _base_priority(tr, holder, a)
    ran_at = [s for s in tr.spans.get(holder, []) if s.state == "RUNNING" and s.end > a and s.start < b]
    at_waiter = [s for s in ran_at if pw >= 0 and s.priority >= pw]
    boosted = [s for s in at_waiter if base is not None and base < pw]
    hcpus = sorted({s.cpu for s in tr.spans.get(holder, []) if s.end > a and s.start < b})
    out["holder"] = {"thread": tr.label(holder), "pid": holder[0], "tid": holder[1],
                     "own_priority": base, "priorities_during_wait": sorted(prios),
                     "cpus": hcpus, "same_process": holder[0] == key[0],
                     "state_ms": {k: round(v, 3) for k, v in sorted(hs.items(), key=lambda kv: -kv[1])}}
    # Who ran on the holder's CPU while it was ready, by priority band.
    middle: dict = defaultdict(float)
    above: dict = defaultdict(float)
    for s in tr.spans.get(holder, []):
        if s.state != "READY" or s.end <= a or s.start >= b:
            continue
        lo, hi = max(a, s.start), min(b, s.end)
        for (k, p), ms in ix.ran(s.cpu, lo, hi).items():
            if k in (holder, key) or k[0] == KERNEL_PID:
                continue
            if s.priority < p < pw:
                middle[(k, p, s.cpu)] += ms
            elif p >= pw:
                above[(k, p, s.cpu)] += ms
    mid_ms, above_ms = sum(middle.values()), sum(above.values())
    out["interference"] = {
        "middle_priority_ms": round(mid_ms, 3), "at_or_above_waiter_ms": round(above_ms, 3),
        "middle": [{"thread": tr.label(k), "priority": p, "cpu": c, "ms": round(ms, 3)}
                   for (k, p, c), ms in sorted(middle.items(), key=lambda kv: -kv[1])[:3]],
        "above": [{"thread": tr.label(k), "priority": p, "cpu": c, "ms": round(ms, 3)}
                  for (k, p, c), ms in sorted(above.items(), key=lambda kv: -kv[1])[:3]]}
    if boosted:
        out["inheritance"] = (f"{tr.label(holder)} ran at priority {max(s.priority for s in boosted)} "
                              f"during the wait (its own priority is {base}): the kernel raised it "
                              "to the waiter's priority")
    # A chain: the holder was itself blocked for part of the wait.
    blocked = {st: ms for st, ms in hs.items() if st in KINDS and ms >= MIN_EFFECT_MS}
    if blocked and depth < 3:
        inner = [x for x in waits(tr, holder, MIN_EFFECT_MS) if x["end"] > a and x["start"] < b]
        if inner:
            out["chain"] = [analyse_wait(tr, holder, x, ix, depth + 1, pw) for x in inner[:2]]
    drops = _dropped(tr, a, b + DROP_REACH_MS, set(hcpus) | {w["cpu"]})
    if drops:
        out["dropped_events"] = [{"t_ms": round(d["t"], 3), "cpu": d["cpu"], "count": d["count"]}
                                 for d in drops]
    significant = max(MIN_EFFECT_MS, SIGNIFICANT * wait_ms)
    below = base is not None and pw >= 0 and base < pw
    if drops:
        cls, why = ("probable_scheduling_interference",
                    "the kernel dropped trace events during this interval, so it may be incomplete")
    elif any(c["classification"] == "confirmed_priority_inversion" for c in out.get("chain", [])):
        c = next(c for c in out["chain"] if c["classification"] == "confirmed_priority_inversion")
        cls, why = ("confirmed_priority_inversion",
                    f"through a chain: {tr.label(holder)} was itself waiting for "
                    f"{c['holder']['thread']}, and {c['reason']}")
    elif mid_ms >= significant and below and not boosted:
        cls, why = ("confirmed_priority_inversion",
                    f"{tr.label(holder)} (priority {base}) held up {tr.label(key)} (priority {pw}) and "
                    f"was kept off its CPU for {mid_ms:.2f} ms by threads between the two priorities")
    elif above_ms >= significant:
        cls, why = ("probable_scheduling_interference",
                    f"{tr.label(holder)} was kept off its CPU for {above_ms:.2f} ms by threads at or "
                    f"above the waiter's priority ({pw}): CPU contention, not a priority inversion")
    elif boosted:
        cls, why = ("no_inversion", out["inheritance"])
    elif at_waiter and not mid_ms:
        cls, why = ("no_inversion",
                    f"{tr.label(holder)} ran at priority {max(s.priority for s in at_waiter)}, the "
                    "waiter's, during the wait, so lower-priority threads could not preempt it (the "
                    "trace does not show it at a lower priority)")
    else:
        cls, why = ("no_inversion",
                    f"{tr.label(holder)} was not held off its CPU by lower-priority work: the wait "
                    f"is its own {hs.get('RUNNING', 0):.2f} ms of running"
                    + (" and its own blocking (see chain)" if blocked else ""))
    out["classification"] = cls
    out["reason"] = why
    top = out["interference"]["middle"][:1] or out["interference"]["above"][:1]
    out["text"] = (f"{tr.label(key)} (priority {pw}) waited {wait_ms:.2f} ms ({w['kind']}) for "
                   f"{tr.label(holder)}: {h['proof']}. "
                   + (f"Meanwhile {top[0]['thread']} (priority {top[0]['priority']}) ran {top[0]['ms']:.2f} ms "
                      f"on CPU {top[0]['cpu']} while the holder was ready. " if top else "")
                   + f"Classification: {cls.replace('_', ' ')} ({why}).")
    if out.get("cut_by_trace"):
        out["text"] += (" The wait continues past the end of the trace; only the recorded part is "
                        "measured." if out["cut_by_trace"] == "end" else
                        " The wait began before the trace started; only the recorded part is measured.")
    return out


# -- whole-trace summary -----------------------------------------------------------------------

def has_links(tr: kev.Trace) -> bool:
    return bool(tr.links)


RANK = {"confirmed_priority_inversion": 0, "probable_scheduling_interference": 1,
        "insufficient_evidence": 2, "no_inversion": 3}


def analyse(tr: kev.Trace, keys=None, min_ms: float = MIN_WAIT_MS, limit: int = 12) -> dict:
    """Every analysed wait of the given threads (default: all but the kernel's and
    tracelogger's), grouped by (thread, kind, holder, classification); the worst wait of
    each group is kept in full with its evidence."""
    if not tr.links:
        return {"source": "thread states only: the trace has no kernel-call or communication "
                          "events, so no wait can be tied to a holder (patterns below are "
                          "probable at best)",
                "waits_by_classification": {}, "groups": [], "more_groups": 0,
                "dropped_event_intervals": len(tr.drops)}
    ix = _Index(tr)
    if keys is None:
        keys = [k for k in tr.spans if k[0] != KERNEL_PID
                and tr.proc_names.get(k[0], "").rsplit("/", 1)[-1] != "tracelogger"]
    groups: dict = {}
    whole = 0
    for k in keys:
        first = min((s.start for s in tr.spans.get(k, [])), default=0.0)
        for w in waits(tr, k, min_ms):
            if w["start"] <= first + 1e-6 and w["end"] >= tr.duration_ms - 1e-6:
                whole += 1                   # blocked for the whole trace: idle, not delayed
                continue
            r = analyse_wait(tr, k, w, ix)
            g = (k, r["kind"], (r.get("holder") or {}).get("thread"), r["classification"])
            cur = groups.setdefault(g, {"count": 0, "total_ms": 0.0, "worst": r})
            cur["count"] += 1
            cur["total_ms"] += r["waited_ms"]
            if r["waited_ms"] > cur["worst"]["waited_ms"]:
                cur["worst"] = r
    out = []
    for (k, kind, holder, cls), g in groups.items():
        w = g["worst"]
        out.append({"thread": tr.label(k), "pid": k[0], "tid": k[1], "kind": kind, "holder": holder,
                    "classification": cls, "waits": g["count"], "total_wait_ms": round(g["total_ms"], 3),
                    "worst_wait_ms": w["waited_ms"], "worst": w})
    # Inversions first, then the most time lost.
    out.sort(key=lambda d: (RANK[d["classification"]], -d["total_wait_ms"]))
    counts: dict = defaultdict(int)
    for d in out:
        counts[d["classification"]] += d["waits"]
    return {
        "source": ("kernel calls and communication events in this trace" if tr.links else
                   "thread states only: the trace has no kernel-call or communication events, "
                   "so no wait can be tied to a holder"),
        "waits_by_classification": dict(counts),
        "blocked_for_whole_trace": whole,
        "groups": out[:limit],
        "more_groups": max(0, len(out) - limit),
        "dropped_event_intervals": len(tr.drops),
    }
