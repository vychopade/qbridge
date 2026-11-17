"""Deterministic diagnosis rules over thread snapshots and deadline reports.

Each finding says what was seen (evidence, with numbers), how sure the rule
is (confidence), and what it could not determine. Rules only use data that
was captured; nothing is inferred from source code.

Input: one or more /dev/qbridge/threads snapshots (JSON, from qbridged) taken
while the workload ran, and optionally the deadline report of a saved run.
"""

from __future__ import annotations

from collections import Counter, defaultdict

RULE_MUTEX_INVERSION = "mutex_priority_inversion"


def _label(t: dict) -> str:
    name = t.get("thread_name") or f"tid {t['tid']}"
    return f"{t['name']}[{t['pid']}] '{name}'"


def _observe_mutex_waits(snapshots: list[list[dict]]) -> dict:
    """Per (pid, sync address): what each snapshot showed about waiters and holder."""
    obs: dict = defaultdict(lambda: {
        "snapshots_with_waiter": 0, "holder_below_waiter": 0, "middle_running": 0,
        "protocols": Counter(), "pid_bits_match": Counter(), "waiters": {}, "holders": {},
        "middle": {}, "max_blocked_ms": 0, "holder_unknown": 0, "shared": False,
    })
    for threads in snapshots:
        by_key = {(t["pid"], t["tid"]): t for t in threads}
        for w in threads:
            b = w.get("blocked_on") or {}
            if w.get("state") != "MUTEX" or "sync" not in b:
                continue
            o = obs[(w["pid"], b["sync"])]
            o["snapshots_with_waiter"] += 1
            o["waiters"][w["tid"]] = w
            o["max_blocked_ms"] = max(o["max_blocked_ms"], w.get("blocked_ms") or 0)
            if b.get("mutex_protocol"):
                o["protocols"][b["mutex_protocol"]] += 1
            if b.get("mutex_shared"):
                o["shared"] = True
            if "mutex_owner_pid_bits_match" in b:
                o["pid_bits_match"][b["mutex_owner_pid_bits_match"]] += 1
            holder = None
            if b.get("mutex_owner_pid_bits_match") and "mutex_owner_tid" in b:
                holder = by_key.get((w["pid"], b["mutex_owner_tid"]))
            if holder is None:
                o["holder_unknown"] += 1
                continue
            o["holders"][holder["tid"]] = holder
            if holder["priority"] < w["priority"]:
                o["holder_below_waiter"] += 1
                # Threads that can run ahead of the holder: higher than the
                # holder, lower than the waiter, ready or running on the
                # holder's CPU.
                mids = [x for x in threads
                        if x["state"] in ("RUNNING", "READY")
                        and holder["priority"] < x["priority"] < w["priority"]
                        and x.get("last_cpu") == holder.get("last_cpu")]
                if mids:
                    o["middle_running"] += 1
                    for x in mids:
                        o["middle"][(x["pid"], x["tid"])] = x
    return obs


def mutex_inversion(snapshots: list[list[dict]], deadline_report: dict | None = None) -> list[dict]:
    """Rule: a higher-priority thread waits for a mutex whose holder runs at a
    lower priority while a thread in between has the CPU."""
    findings = []
    for (pid, sync), o in _observe_mutex_waits(snapshots).items():
        if o["holder_below_waiter"] == 0:
            continue
        waiter = max(o["waiters"].values(), key=lambda t: t["priority"])
        holder = min(o["holders"].values(), key=lambda t: t["priority"])
        protocol, _ = o["protocols"].most_common(1)[0] if o["protocols"] else (None, 0)
        middle = sorted(o["middle"].values(), key=lambda t: -t["priority"])
        n = len(snapshots)

        evidence = [
            f"In {o['snapshots_with_waiter']} of {n} snapshots {_label(waiter)} (priority "
            f"{waiter['priority']}) was blocked on the mutex at {sync}, for up to "
            f"{o['max_blocked_ms']} ms.",
            f"The mutex holder was {_label(holder)} at priority {holder['priority']} "
            f"(read from the mutex's owner word in process memory) in "
            f"{o['holder_below_waiter']} of those snapshots: below the waiter, so it was not "
            f"running at the waiter's priority.",
        ]
        if middle:
            evidence.append(
                f"In {o['middle_running']} snapshot(s) a thread between the two priorities had the "
                f"holder's CPU: " + ", ".join(f"{_label(x)} at priority {x['priority']} ({x['state']})"
                                              for x in middle) + ".")
        if protocol:
            evidence.append(f"The mutex's protocol flags say {protocol} (from its count word, "
                            f"flag _NTO_SYNC_PRIONONE)" if protocol == "PTHREAD_PRIO_NONE"
                            else f"The mutex's protocol flags say {protocol}.")
        dl = None
        if deadline_report:
            roots = deadline_report.get("chain_root_causes", {})
            lock_rooted = roots.get("lock_wait", 0)
            total = deadline_report.get("miss_chains", 0)
            dl = {"misses": deadline_report.get("misses"), "lock_rooted_runs": lock_rooted,
                  "runs": total}
            if total:
                evidence.append(
                    f"The deadline report for {deadline_report.get('task')} shows "
                    f"{deadline_report['misses']} misses in {total} run(s) of consecutive misses; "
                    f"{lock_rooted} of those runs started with a long lock wait (worst lock wait "
                    f"{deadline_report['parts_us']['lock_wait']['max'] / 1000:.1f} ms).")

        # Confidence: each independent piece of evidence raises it.
        points = 0
        points += 1 if o["holder_below_waiter"] else 0
        points += 1 if middle else 0
        points += 1 if protocol == "PTHREAD_PRIO_NONE" else 0
        points += 1 if dl and dl["runs"] and dl["lock_rooted_runs"] >= dl["runs"] / 2 else 0
        confidence = "high" if points >= 3 else "medium" if points == 2 else "low"

        unknown = [
            f"Snapshots are instants: {n} were taken. They show these states happened, not how "
            "often or for how long overall.",
            "The holder is decoded from the mutex's owner word using a layout observed on QNX 8.0 "
            "(pid low bits and tid); it is not a documented interface. The pid bits matched in "
            f"{o['pid_bits_match'].get(True, 0)} of {sum(o['pid_bits_match'].values())} reads.",
            "Which source line created or locks this mutex: the rule sees addresses and threads, "
            "not code.",
            "Whether other locks or resources also delay the waiter.",
            "A worst case: finite runs cannot prove the deadline will always be met after a fix.",
        ]
        if protocol is None:
            unknown.insert(0, "The mutex's protocol (its memory could not be read).")
        if o["shared"]:
            unknown.insert(0, "The mutex is process-shared; its holder may be in another process.")
        if not dl:
            unknown.append("Whether this causes the deadline misses: no deadline report was "
                           "given (run qnx_measure and pass its run_id).")

        if protocol == "PTHREAD_PRIO_NONE":
            fix = ("Let the mutex use priority inheritance, the QNX default: create it with "
                   "PTHREAD_PRIO_INHERIT (or without setting a protocol) instead of "
                   "PTHREAD_PRIO_NONE. The holder then runs at the waiter's priority while the "
                   "waiter is blocked, so a middle-priority thread can no longer delay it.")
        else:
            fix = ("Find out why the holder does not run at the waiter's priority (check the "
                   "mutex protocol and any priority changes), or shorten the critical section.")

        findings.append({
            "rule": RULE_MUTEX_INVERSION,
            "title": f"Priority inversion on a mutex in {waiter['name']}[{pid}]",
            "confidence": confidence,
            "explanation": (
                f"{_label(waiter)} (priority {waiter['priority']}) waits for a mutex held by "
                f"{_label(holder)} (priority {holder['priority']})"
                + (f", while {', '.join(_label(x) for x in middle)} (priority "
                   f"{', '.join(str(x['priority']) for x in middle)}) keeps the CPU" if middle else "")
                + ". The holder cannot finish its critical section until the middle-priority "
                  "work stops, so the high-priority thread waits for unrelated work."),
            "evidence": evidence,
            "could_not_determine": unknown,
            "suggested_fix": fix,
            "subject": {"pid": pid, "sync": sync, "protocol": protocol,
                        "waiter": {k: waiter[k] for k in ("tid", "thread_name", "priority")},
                        "holder": {k: holder[k] for k in ("tid", "thread_name", "priority")},
                        "middle": [{k: x[k] for k in ("pid", "tid", "thread_name", "priority")}
                                   for x in middle]},
            "observations": {"snapshots": n, "with_waiter": o["snapshots_with_waiter"],
                             "holder_below_waiter": o["holder_below_waiter"],
                             "middle_running": o["middle_running"],
                             "holder_unknown": o["holder_unknown"]},
        })
    return findings


def inheritance_observed(snapshots: list[list[dict]]) -> list[dict]:
    """Mutex waits where the holder was running at least at the waiter's
    priority although its own (real) priority is lower: the kernel raised it,
    which is priority inheritance working."""
    seen = []
    for i, threads in enumerate(snapshots):
        by_key = {(t["pid"], t["tid"]): t for t in threads}
        for w in threads:
            b = w.get("blocked_on") or {}
            if w.get("state") != "MUTEX" or not b.get("mutex_owner_pid_bits_match"):
                continue
            h = by_key.get((w["pid"], b.get("mutex_owner_tid")))
            if h and h["priority"] >= w["priority"] > h.get("real_priority", h["priority"]):
                seen.append({"snapshot": i, "pid": w["pid"], "sync": b.get("sync"),
                             "protocol": b.get("mutex_protocol"),
                             "waiter": w.get("thread_name"), "waiter_priority": w["priority"],
                             "holder": h.get("thread_name"), "holder_priority": h["priority"],
                             "holder_real_priority": h.get("real_priority"),
                             "holder_state": h["state"]})
    return seen


def diagnose(snapshots: list[list[dict]], deadline_report: dict | None = None) -> dict:
    """Run every rule; say plainly when nothing was found."""
    findings = mutex_inversion(snapshots, deadline_report)
    inherited = inheritance_observed(snapshots)
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order[f["confidence"]])
    if findings:
        summary = f"{len(findings)} finding(s); most likely: {findings[0]['title']} " \
                  f"({findings[0]['confidence']} confidence)."
    else:
        waits = sum(1 for s in snapshots for t in s if t.get("state") == "MUTEX")
        summary = (f"No priority inversion seen in {len(snapshots)} snapshot(s)"
                   + (f" ({waits} brief mutex waits, all with the holder at or above the "
                      "waiter's priority or the holder unknown)" if waits else "")
                   + ". This does not prove there is none; it was not observed.")
        if deadline_report and deadline_report.get("misses"):
            summary += (f" The deadline report still shows {deadline_report['misses']} misses: "
                        f"{deadline_report.get('summary', '')}")
    if inherited:
        x = inherited[0]
        summary += (f" Priority inheritance was seen working {len(inherited)} time(s): e.g. "
                    f"'{x['holder']}' held a {x['protocol']} mutex and ran at priority "
                    f"{x['holder_priority']} (its own priority is {x['holder_real_priority']}) "
                    f"while '{x['waiter']}' (priority {x['waiter_priority']}) waited.")
    return {"summary": summary, "findings": findings, "inheritance_observed": inherited,
            "snapshots": len(snapshots)}
