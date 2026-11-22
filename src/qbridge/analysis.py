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


RULE_CPU_PREEMPTION = "cpu_preemption"

# Kernel threads (procnto, pid 1: interrupt and clock threads) run briefly at
# very high priority all the time; they are not the kind of CPU-heavy thread
# this rule is about, so they are not counted as the cause.
_KERNEL_PID = 1


def _mutex_holders_waited_on(snap: list[dict]) -> set[tuple[int, int]]:
    """(pid, tid) of threads holding a mutex that a higher-priority thread is
    blocked on in this snapshot (holder decoded and pid bits matched)."""
    by_key = {(t["pid"], t["tid"]): t for t in snap}
    out = set()
    for w in snap:
        b = w.get("blocked_on") or {}
        if w.get("state") == "MUTEX" and b.get("mutex_owner_pid_bits_match"):
            h = by_key.get((w["pid"], b.get("mutex_owner_tid")))
            if h is not None and h.get("real_priority", h["priority"]) < w["priority"]:
                out.add((h["pid"], h["tid"]))
    return out


def _task_threads(snapshots: list[list[dict]], deadline_report: dict | None) -> set[tuple[int, int]]:
    """Threads the rule examines: the deadline task if its name is known, else
    every thread seen READY."""
    name = (deadline_report or {}).get("task")
    keys = set()
    for snap in snapshots:
        for t in snap:
            if name and t.get("thread_name") == name:
                keys.add((t["pid"], t["tid"]))
            elif not name and t.get("state") == "READY" and t["pid"] != _KERNEL_PID:
                # Kernel threads (idle threads above all) are READY at low
                # priority whenever anything else runs; they are not tasks.
                keys.add((t["pid"], t["tid"]))
    return keys


def cpu_preemption(snapshots: list[list[dict]], deadline_report: dict | None = None,
                   timing_checks: list[dict] | None = None) -> list[dict]:
    """Rule: a deadline task is ready to run but a thread of higher priority
    keeps its CPU, and its misses come from waiting for the CPU, not a lock.

    Required: at least one snapshot with the task READY while a non-kernel
    thread of higher priority is RUNNING on the task's CPU. Everything else
    raises or lowers confidence.
    """
    findings = []
    for key in sorted(_task_threads(snapshots, deadline_report)):
        seen, ready, running_above, as_holder = 0, 0, 0, 0
        runners: dict = {}
        task = None
        for snap in snapshots:
            me = next((t for t in snap if (t["pid"], t["tid"]) == key), None)
            if me is None:
                continue
            seen += 1
            task = me
            if me.get("state") != "READY":
                continue
            if key in _mutex_holders_waited_on(snap) or key in _servers_waited_on(snap):
                # A preempted lock holder (or server) that a higher-priority
                # thread waits for is the middle step of a priority inversion;
                # that is the mutex (or message-passing) rule's finding, not
                # CPU overload.
                as_holder += 1
                continue
            ready += 1
            above = [x for x in snap
                     if x.get("state") == "RUNNING" and x["pid"] != _KERNEL_PID
                     and x.get("priority", 0) > me["priority"]
                     and x.get("last_cpu") == me.get("last_cpu")]
            if above:
                running_above += 1
                for x in above:
                    r = runners.setdefault((x["pid"], x["tid"]), {"thread": x, "count": 0})
                    r["count"] += 1
        if running_above == 0 or task is None:
            continue
        top = max(runners.values(), key=lambda r: r["count"])
        runner = top["thread"]
        n = len(snapshots)

        evidence = [
            f"{_label(task)} (priority {task['priority']}) was READY (able to run, not running) "
            f"in {ready} of the {seen} snapshots it appeared in.",
            f"In {running_above} of those, a thread with a higher priority was RUNNING on its CPU "
            f"(CPU {task.get('last_cpu')}): mostly {_label(runner)} at priority "
            f"{runner['priority']} ({top['count']} time(s)).",
        ]
        points = 1
        if running_above >= 3 and running_above >= ready / 2:
            points += 1
            evidence.append("This was the usual situation, not a one-off: the higher-priority "
                            f"thread was running in {running_above} of {ready} READY snapshots.")

        dl = None
        if deadline_report and deadline_report.get("misses"):
            roots = deadline_report.get("chain_root_causes", {})
            cpu_rooted = roots.get("latency", 0) + roots.get("preempted", 0)
            lock_rooted = roots.get("lock_wait", 0)
            runs_ = deadline_report.get("miss_chains", 0)
            worst_lock = deadline_report["parts_us"]["lock_wait"]["max"]
            worst_wait = max(deadline_report["parts_us"]["latency"]["max"],
                             deadline_report["parts_us"]["preempted"]["max"])
            # The 99th percentile is the typical bad case; unlike the single
            # worst cycle it is not moved by one outlier (e.g. one VM stall).
            p99_wait = max(deadline_report["parts_us"]["latency"].get("p99", 0.0),
                           deadline_report["parts_us"]["preempted"].get("p99", 0.0))
            dl = {"cpu_rooted": cpu_rooted, "lock_rooted": lock_rooted, "runs": runs_,
                  "worst_lock": worst_lock, "worst_wait": worst_wait, "p99_wait": p99_wait}
            evidence.append(
                f"The deadline report for {deadline_report.get('task')} shows "
                f"{deadline_report['misses']} misses in {runs_} run(s) of consecutive misses; "
                f"{cpu_rooted} of those runs started with waiting for the CPU (late start or "
                f"preempted) and {lock_rooted} with a lock wait. Worst wait for the CPU "
                f"{worst_wait / 1000:.1f} ms; worst lock wait {worst_lock / 1000:.1f} ms.")
            if runs_ and cpu_rooted >= runs_ / 2 and lock_rooted < runs_ / 2:
                points += 1
        tc = next((t for t in timing_checks or [] if t.get("cpu") == task.get("last_cpu")), None)
        if tc and dl:
            p99, worst, noise = dl["p99_wait"], dl["worst_wait"], tc["max_late_us"]
            if p99 > 2 * noise:
                points += 1
                evidence.append(
                    f"The 99th-percentile wait for the CPU ({p99 / 1000:.1f} ms; worst "
                    f"{worst / 1000:.1f} ms) is more than twice the worst timer lateness measured on "
                    f"CPU {tc['cpu']} with nothing else running ({noise / 1000:.1f} ms), so "
                    "virtual-machine noise alone does not explain it.")
            else:
                evidence.append(
                    f"Caution: the 99th-percentile wait for the CPU ({p99 / 1000:.1f} ms; worst "
                    f"{worst / 1000:.1f} ms) is within twice the measured timer lateness of CPU "
                    f"{tc['cpu']} ({noise / 1000:.1f} ms); part of it may be virtual-machine noise.")
            if worst > 4 * max(p99, 1.0):
                evidence.append(
                    f"One wait ({worst / 1000:.1f} ms) is far above the rest (99th percentile "
                    f"{p99 / 1000:.1f} ms). It is not used as evidence: one outlier can come from "
                    "the virtual machine or from other activity and is not explained by this rule.")
        confidence = "high" if points >= 3 else "medium" if points == 2 else "low"

        unknown = [
            f"Snapshots are instants: {n} were taken. They show the higher-priority thread had the "
            "CPU at those moments, not for how long overall.",
            "Kernel threads (procnto) are not counted as the cause; their CPU use is not analysed.",
            "Whether the higher-priority thread's work is legitimate: the rule sees priorities and "
            "states, not what the threads are for.",
            "A worst case: finite runs cannot prove the deadline will always be met after a fix.",
        ]
        if not dl:
            unknown.append("Whether this causes the deadline misses: no deadline report was given.")
        if not tc:
            unknown.append(f"How much of the late starts is virtual-machine timer noise: no timing "
                           f"check of CPU {task.get('last_cpu')} was saved (run qnx_timing_check).")

        fix = (f"Run {_label(task)} at a higher priority than {_label(runner)} (now "
               f"{task['priority']} vs {runner['priority']}), or lower the CPU-heavy thread's "
               "priority. On QNX a higher number is a higher priority and a FIFO thread keeps the "
               "CPU until it blocks, so a deadline-critical thread must sit above CPU-heavy work "
               "on its CPU. This is a scheduling-parameter change; no lock or code change is "
               "involved.")
        findings.append({
            "rule": RULE_CPU_PREEMPTION,
            "title": f"CPU preemption: {task['name']}[{task['pid']}] waits for a higher-priority "
                     f"thread",
            "confidence": confidence,
            "explanation": (
                f"{_label(task)} (priority {task['priority']}) is ready to run but "
                f"{_label(runner)} (priority {runner['priority']}) holds its CPU. The scheduler "
                "always runs the highest-priority ready thread, so the deadline task waits until "
                "the CPU-heavy thread stops. No lock is involved."),
            "evidence": evidence,
            "could_not_determine": unknown,
            "suggested_fix": fix,
            "subject": {"pid": task["pid"], "cpu": task.get("last_cpu"),
                        "task": {k: task[k] for k in ("tid", "thread_name", "priority")},
                        "runner": {k: runner[k] for k in ("pid", "tid", "thread_name", "priority")},
                        "middle": [{k: runner[k] for k in ("pid", "tid", "thread_name", "priority")}]},
            "observations": {"snapshots": n, "seen": seen, "ready": ready,
                             "higher_running_while_ready": running_above,
                             "skipped_as_inversion_holder": as_holder},
        })
    return findings


RULE_MSGPASS_INVERSION = "msgpass_priority_inversion"

_MSG_STATES = ("SEND", "REPLY")


def _server_workers(snap: list[dict], client: dict) -> list[dict]:
    """Threads of the server process a SEND/REPLY-blocked client waits on that
    could be working for it: not waiting for messages themselves, not blocked
    on another server. Only a server in another process is examined: inside one
    process the snapshot cannot tell which thread received the message."""
    spid = (client.get("blocked_on") or {}).get("pid")
    if spid is None or spid == client["pid"] or spid == _KERNEL_PID:
        return []
    return [t for t in snap if t["pid"] == spid and t.get("state") != "RECEIVE"
            and t.get("state") not in _MSG_STATES]


def _servers_waited_on(snap: list[dict]) -> set[tuple[int, int]]:
    """(pid, tid) of server threads running below a client that waits on them."""
    out = set()
    for c in snap:
        if c.get("state") in _MSG_STATES:
            for w in _server_workers(snap, c):
                if w["priority"] < c["priority"]:
                    out.add((w["pid"], w["tid"]))
    return out


def msgpass_inversion(snapshots: list[list[dict]], deadline_report: dict | None = None) -> list[dict]:
    """Rule: a client is SEND- or REPLY-blocked on a server process whose
    thread runs below the client's priority, while a thread in between has the
    server's CPU. On QNX a receiving server thread normally takes the client's
    priority, so this means inheritance is not happening (for example a
    channel created with _NTO_CHF_FIXED_PRIORITY)."""
    name = (deadline_report or {}).get("task")
    obs: dict = {}
    for snap in snapshots:
        for c in snap:
            if c.get("state") not in _MSG_STATES or (name and c.get("thread_name") != name):
                continue
            workers = _server_workers(snap, c)
            if not workers:
                continue
            spid = c["blocked_on"]["pid"]
            o = obs.setdefault((c["pid"], c["tid"], spid), {
                "with_wait": 0, "server_below": 0, "middle_running": 0, "client": c,
                "servers": {}, "middle": {}, "max_blocked_ms": 0, "states": Counter()})
            o["with_wait"] += 1
            o["states"][c["state"]] += 1
            o["client"] = c
            o["max_blocked_ms"] = max(o["max_blocked_ms"], c.get("blocked_ms") or 0)
            top = max(workers, key=lambda t: t["priority"])
            if top["priority"] >= c["priority"]:
                continue
            o["server_below"] += 1
            o["servers"][top["tid"]] = top
            mids = [x for x in snap
                    if x.get("state") in ("RUNNING", "READY") and x["pid"] != _KERNEL_PID
                    and top["priority"] < x["priority"] < c["priority"]
                    and x.get("last_cpu") == top.get("last_cpu")]
            if mids:
                o["middle_running"] += 1
                for x in mids:
                    o["middle"][(x["pid"], x["tid"])] = x

    findings = []
    n = len(snapshots)
    for (cpid, _ctid, spid), o in obs.items():
        if o["server_below"] == 0:
            continue
        client = o["client"]
        server = min(o["servers"].values(), key=lambda t: t["priority"])
        middle = sorted(o["middle"].values(), key=lambda t: -t["priority"])
        states = ", ".join(f"{k} {v}" for k, v in sorted(o["states"].items()))
        evidence = [
            f"In {o['with_wait']} of {n} snapshots {_label(client)} (priority "
            f"{client['priority']}) was waiting on a message to process {server['name']}[{spid}] "
            f"({states}), for up to {o['max_blocked_ms']} ms.",
            f"In {o['server_below']} of those, the server's working thread {_label(server)} ran "
            f"at priority {server['priority']}, below its client. On QNX a server thread that "
            f"receives a message normally runs at the client's priority, so it was not "
            f"inheriting it.",
        ]
        if middle:
            evidence.append(
                f"In {o['middle_running']} snapshot(s) a thread between the two priorities had the "
                f"server's CPU: " + ", ".join(f"{_label(x)} at priority {x['priority']} "
                                              f"({x['state']})" for x in middle) + ".")
        dl = None
        if deadline_report and deadline_report.get("misses"):
            roots = deadline_report.get("chain_root_causes", {})
            srv = roots.get("server_wait", 0)
            runs_ = deadline_report.get("miss_chains", 0)
            worst = deadline_report["parts_us"].get("server_wait", {}).get("max", 0.0)
            dl = {"server_rooted": srv, "runs": runs_, "worst_server_wait": worst}
            evidence.append(
                f"The deadline report for {deadline_report.get('task')} shows "
                f"{deadline_report['misses']} misses in {runs_} run(s) of consecutive misses; "
                f"{srv} of those runs started with a long wait for a server's reply (worst "
                f"{worst / 1000:.1f} ms from send to reply; the server's usual answer time is "
                f"{(deadline_report.get('server_budget_us') or 0) / 1000:.1f} ms).")

        points = 1
        points += 1 if middle else 0
        points += 1 if o["server_below"] >= 3 and o["server_below"] >= o["with_wait"] / 2 else 0
        points += 1 if dl and dl["runs"] and dl["server_rooted"] >= dl["runs"] / 2 else 0
        confidence = "high" if points >= 3 else "medium" if points == 2 else "low"

        unknown = [
            f"Snapshots are instants: {n} were taken, and qbridged reads processes one after "
            "another, so client and server are not read at exactly the same moment.",
            "Why the server does not inherit the client's priority: qbridge cannot read a "
            "channel's flags. _NTO_CHF_FIXED_PRIORITY on the server's channel has this effect; so "
            "would a server that sets its own priority after receiving.",
            "Which server thread received this client's message, if the server process has "
            "several working threads: the snapshot names the process, not the thread.",
            "A worst case: finite runs cannot prove the deadline will always be met after a fix.",
        ]
        if not dl:
            unknown.append("Whether this causes the deadline misses: no deadline report was given.")
        fix = ("Let the server inherit its clients' priority, the QNX default: create its channel "
               "without _NTO_CHF_FIXED_PRIORITY (and do not lower the server thread's priority "
               "after MsgReceive). The server then works for a client at the client's priority, so "
               "a middle-priority thread can no longer delay the reply. If the server must keep a "
               "fixed priority, run it above the threads that compete for its CPU.")
        findings.append({
            "rule": RULE_MSGPASS_INVERSION,
            "title": f"Priority inversion through message passing: {client['name']}[{cpid}] waits "
                     f"on {server['name']}[{spid}]",
            "confidence": confidence,
            "explanation": (
                f"{_label(client)} (priority {client['priority']}) waits for a reply from "
                f"{_label(server)}, which serves it at priority {server['priority']}"
                + (f", while {', '.join(_label(x) for x in middle)} (priority "
                   f"{', '.join(str(x['priority']) for x in middle)}) keeps the server's CPU"
                   if middle else "")
                + ". The reply cannot come until the middle-priority work stops, so the "
                  "high-priority client waits for unrelated work. No mutex is involved."),
            "evidence": evidence,
            "could_not_determine": unknown,
            "suggested_fix": fix,
            "subject": {"pid": cpid, "server_pid": spid,
                        "client": {k: client[k] for k in ("tid", "thread_name", "priority")},
                        "server": {k: server[k] for k in ("pid", "tid", "thread_name", "priority")},
                        "middle": [{k: x[k] for k in ("pid", "tid", "thread_name", "priority")}
                                   for x in middle]},
            "observations": {"snapshots": n, "with_wait": o["with_wait"],
                             "server_below_client": o["server_below"],
                             "middle_running": o["middle_running"]},
        })
    return findings


def msgpass_inheritance_observed(snapshots: list[list[dict]]) -> list[dict]:
    """Message waits where the server thread runs at least at its client's
    priority although its own priority is lower: inheritance working."""
    seen = []
    for i, snap in enumerate(snapshots):
        for c in snap:
            if c.get("state") not in _MSG_STATES:
                continue
            for w in _server_workers(snap, c):
                if w["priority"] >= c["priority"] > w.get("real_priority", w["priority"]):
                    seen.append({"snapshot": i, "client": c.get("thread_name"),
                                 "client_priority": c["priority"], "server": w.get("thread_name"),
                                 "server_pid": w["pid"], "server_priority": w["priority"],
                                 "server_real_priority": w.get("real_priority"),
                                 "server_state": w["state"]})
    return seen


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


def diagnose(snapshots: list[list[dict]], deadline_report: dict | None = None,
             timing_checks: list[dict] | None = None) -> dict:
    """Run every rule; say plainly when nothing was found."""
    findings = (mutex_inversion(snapshots, deadline_report)
                + msgpass_inversion(snapshots, deadline_report)
                + cpu_preemption(snapshots, deadline_report, timing_checks))
    inherited = inheritance_observed(snapshots)
    msg_inherited = msgpass_inheritance_observed(snapshots)
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order[f["confidence"]])
    if findings:
        summary = f"{len(findings)} finding(s); most likely: {findings[0]['title']} " \
                  f"({findings[0]['confidence']} confidence)."
    else:
        waits = sum(1 for s in snapshots for t in s if t.get("state") == "MUTEX")
        summary = (f"No priority inversion or CPU preemption seen in {len(snapshots)} snapshot(s) "
                   "(rules: mutex and message-passing priority inversion, CPU preemption)"
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
    if msg_inherited:
        x = msg_inherited[0]
        summary += (f" Message-passing priority inheritance was seen working "
                    f"{len(msg_inherited)} time(s): e.g. server '{x['server']}' ran at priority "
                    f"{x['server_priority']} (its own priority is {x['server_real_priority']}) while "
                    f"'{x['client']}' (priority {x['client_priority']}) waited for its reply.")
    out = {"summary": summary, "findings": findings, "inheritance_observed": inherited,
           "snapshots": len(snapshots)}
    if msg_inherited:
        out["msgpass_inheritance_observed"] = msg_inherited
    return out
