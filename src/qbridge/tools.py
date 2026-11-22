"""The qbridge tools. The CLI and the MCP server both call these.

Each tool returns a JSON-friendly dict. Tools that change the target check
the Policy first.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import build as build_mod
from . import analysis, audit, commands, crash, deadlines, runs
from .parsers import pidin, qbridged, slog2
from .policy import Policy, PolicyError
from .target import Target, TargetError

# QNX's /system/bin/timeout exits 124 when it kills the command (checked on target).
TIMEOUT_EXIT = 124
MAX_OUTPUT = 20_000


@dataclass
class Session:
    target: Target = field(default_factory=Target.from_env)
    policy: Policy = field(default_factory=Policy.from_env)

    def __post_init__(self):
        # The command allowlist and the policy use the same deploy folder.
        self.target.deploy_root = self.policy.deploy_root


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return f"[... {len(text) - MAX_OUTPUT} characters cut ...]\n" + text[-MAX_OUTPUT:]


def info(s: Session) -> dict:
    r = s.target.run(commands.script("info", s.target.deploy_root))
    return {
        "host": s.target.resolve_host(),
        "write_allowed": s.policy.allow_write,
        "deploy_root": s.policy.deploy_root,
        "output": r.stdout.strip(),
    }


def _read_threads(s: Session) -> tuple[list[pidin.Thread], str]:
    """Threads from qbridged if it is running on the target, else from pidin."""
    r = s.target.run(["cat", qbridged.THREADS_PATH])
    if r.ok and r.stdout.strip():
        try:
            return qbridged.parse_threads(r.stdout), "qbridged"
        except (ValueError, KeyError):
            pass  # fall back to pidin below
    r = s.target.run(pidin.PIDIN_ARGS)
    if not r.ok:
        raise TargetError(f"pidin failed: {r.stderr.strip()}")
    return pidin.parse_threads(r.stdout), "pidin"


def ps(s: Session, process: str | None = None) -> dict:
    threads, source = _read_threads(s)
    # Analyse every thread so edges and findings keep the other side's details.
    edges = pidin.blocked_graph(threads)
    cycles = pidin.find_cycles(edges)
    deadlocks = pidin.describe_deadlocks(cycles, threads)
    inversions = pidin.find_inversions(threads)
    if process:
        want = process.lower()
        threads = [t for t in threads if want in t.name.lower() or str(t.pid) == process]
        pids = {t.pid for t in threads}
        edges = [e for e in edges if e["from_pid"] in pids or e["to_pid"] in pids]
        cycles = [c for c in cycles if pids & set(c)]
        deadlocks = [d for d in deadlocks if pids & set(d["pids"])]
        inversions = [i for i in inversions
                      if pids & {i["client"]["pid"], i["server"]["pid"]}
                      | {p["pid"] for p in i["preempted_by"]}]
    return {"source": source, "processes": pidin.processes(threads), "blocked_on": edges,
            "cycles": cycles, "deadlocks": deadlocks, "inversions": inversions}


def logs(s: Session, process: str | None = None, pid: int | None = None,
         since: float | None = None, severity: str | None = None,
         grep: str | None = None, limit: int = 200) -> dict:
    r = s.target.run(slog2.SLOG2_ARGS)
    if not r.ok:
        raise TargetError(f"slog2info failed: {r.stderr.strip()}")
    entries = slog2.filter_entries(slog2.parse(r.stdout), process=process, pid=pid,
                                   since=since, min_severity=severity, contains=grep, limit=limit)
    return {"count": len(entries), "entries": [e.to_dict() for e in entries]}


def build(s: Session, path: str, make_target: str | None = None, output: str | None = None) -> dict:
    # Runs on the host only, so it is allowed in read-only mode.
    return build_mod.build(path, make_target=make_target, output=output).to_dict()


def deploy(s: Session, local_path: str, name: str | None = None) -> dict:
    s.policy.require_write("deploy")
    local = Path(local_path).resolve()
    if not local.is_file():
        raise FileNotFoundError(f"{local} is not a file")
    remote = s.policy.deploy_path(name or local.name)
    s.target.run(["mkdir", "-p", posixpath.dirname(remote)])
    # QNX refuses to overwrite a running executable ("dest open ... Failure"),
    # so copy beside it and rename over it; rename works while it runs.
    tmp = remote + ".new"
    s.target.put(local, tmp)
    r = s.target.run(commands.script("install", s.target.deploy_root, tmp=tmp, dest=remote))
    if not r.ok:
        raise TargetError(f"installing {remote} failed: {r.stderr.strip()}")
    return {"local": str(local), "remote": remote, "bytes": local.stat().st_size}


MAX_BACKGROUND_S = 3600
KILL_SIGNALS = commands.SIGNALS


def run(s: Session, path: str, args: list[str] | None = None, timeout: int = 30,
        background: bool = False) -> dict:
    s.policy.require_write("run")
    remote = s.policy.deploy_path(path)
    if background:
        return _run_background(s, remote, args or [], timeout)
    argv = ["timeout", str(int(timeout)), remote, *(args or [])]
    r = s.target.run(argv, timeout=timeout + 15)
    out = {
        "command": r.command,
        "exit_code": r.returncode,
        "timed_out": r.returncode == TIMEOUT_EXIT,
        "stdout": _clip(r.stdout),
        "stderr": _clip(r.stderr),
    }
    # The shell reports death by signal N as exit code 128+N (seen on target: 139 = SIGSEGV).
    if r.returncode > 128 and r.returncode != TIMEOUT_EXIT:
        signo = r.returncode - 128
        out["signal"] = crash.SIGNALS.get(signo, f"signal {signo}")
        out["hint"] = "The program was killed by a signal; dumper may have written a core. Use qnx_crash."
    return out


def _run_background(s: Session, remote: str, args: list[str], timeout: int) -> dict:
    """Start a program and return at once. It is still stopped after `timeout` s.

    Output goes to <program>.log beside it; qnx_kill returns that log.
    stdin/stdout/stderr are redirected so the ssh session is not held open.
    """
    t = max(1, min(int(timeout), MAX_BACKGROUND_S))
    log = remote + ".log"
    r = s.target.run(commands.script("start_background", s.target.deploy_root,
                                     argv=["timeout", str(t), remote, *args], log=log))
    pid = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if not r.ok or not pid.isdigit():
        raise TargetError(f"could not start {remote} in the background: {r.stderr.strip()}")
    return {
        "background": True,
        "program": remote,
        "timeout_pid": int(pid),
        "stops_after_s": t,
        "log": log,
        "hint": "Watch it with qnx_ps or qnx_events. Stop it with qnx_kill, which also returns its output.",
    }


def _deployed_processes(s: Session) -> list[dict]:
    """Every process on the target with its program path, read from /proc/<pid>/exefile."""
    r = s.target.run(commands.script("list_exefiles", s.target.deploy_root))
    procs = []
    for line in r.stdout.splitlines():
        pid, _, path = line.partition("|")
        path = path.replace("\x00", "").strip()
        if pid.isdigit() and path:
            procs.append({"pid": int(pid), "path": path, "name": path.rsplit("/", 1)[-1]})
    return procs


def kill(s: Session, pid: int | None = None, name: str | None = None,
         signal: str = "TERM") -> dict:
    """Send a signal to programs that qbridge deployed (only those).

    Pick them by pid or by program name. ABRT makes dumper write a core file,
    which is a way to see where a hung (e.g. deadlocked) program is stuck.
    """
    s.policy.require_write("kill")
    sig = signal.upper().removeprefix("SIG")
    if sig not in KILL_SIGNALS:
        raise ValueError(f"signal {signal!r} not allowed; use one of {', '.join(sorted(KILL_SIGNALS))}")
    if pid is None and not name:
        raise ValueError("give a pid or a program name")

    root = s.policy.deploy_root.rstrip("/") + "/"
    procs = _deployed_processes(s)
    if pid is not None:
        match = [p for p in procs if p["pid"] == pid]
        if not match:
            raise ValueError(f"no process {pid}")
    else:
        match = [p for p in procs if p["name"] == name]
        if not match:
            raise ValueError(f"no running program named {name!r}")
    outside = [p for p in match if not p["path"].startswith(root)]
    if outside:
        p = outside[0]
        raise PolicyError(f"process {p['pid']} runs {p['path']}, outside the deploy folder "
                          f"{s.policy.deploy_root}; qbridge only signals programs it deployed")

    pids = [str(p["pid"]) for p in match]
    r = s.target.run(["kill", "-s", sig, *pids])
    out: dict = {"signal": "SIG" + sig, "processes": match, "ok": r.ok}
    if not r.ok:
        out["error"] = r.stderr.strip()
    logs = {}
    for path in sorted({p["path"] for p in match}):
        lr = s.target.run(commands.script("log_tail", s.target.deploy_root, log=path + ".log"))
        if lr.stdout.strip():
            logs[path + ".log"] = lr.stdout
    if logs:
        out["logs"] = logs
    if sig in ("ABRT", "QUIT"):
        out["hint"] = "dumper should have written a core file; use qnx_crash to see where it was stuck."
    return out


def _remote_sha256(s: Session, path: str) -> str | None:
    r = s.target.run(["openssl", "dgst", "-sha256", path])
    m = re.search(r"=\s*([0-9a-f]{64})", r.stdout)
    return m.group(1) if m else None


def measure(s: Session, program: str, seconds: int = 10, args: list[str] | None = None,
            label: str | None = None, deadline_us: float | None = None,
            period_us: float | None = None) -> dict:
    """Run a periodic workload for a fixed time and report its deadline statistics.

    The program must follow the qbridge RT workload convention (rtdemo does):
    it accepts `-d <seconds> -o <samples.csv>`, writes one CSV row per cycle
    (release_us, response_us and optionally backlog_us, latency_us,
    lock_wait_us, exec_us) and prints a JSON summary line with period_us and
    deadline_us. The run is saved on the host so it can be compared later.
    """
    s.policy.require_write("measure")
    seconds = max(1, min(int(seconds), 600))
    remote = s.policy.deploy_path(program)
    csv_remote = remote + ".samples.csv"
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    sha = _remote_sha256(s, remote)
    out = run(s, program, ["-d", str(seconds), "-o", csv_remote, *(args or [])],
              timeout=seconds + 30)
    if out["exit_code"] != 0:
        raise TargetError(f"{program} exited with {out['exit_code']}"
                          + (f" ({out['signal']})" if "signal" in out else "")
                          + f": {out['stderr'].strip()[-500:]}")
    summary = None
    for line in reversed(out["stdout"].strip().splitlines()):
        try:
            summary = json.loads(line)
            break
        except ValueError:
            continue
    deadline = deadline_us or (summary or {}).get("deadline_us")
    period = period_us or (summary or {}).get("period_us")
    if not deadline:
        raise ValueError("deadline unknown: the program printed none; pass deadline_us")
    csv_text = s.target.run(["cat", csv_remote]).stdout
    samples = deadlines.parse_samples_csv(csv_text)
    if not samples:
        raise TargetError(f"no samples in {csv_remote}")
    task = (summary or {}).get("task") or posixpath.basename(remote)
    report = deadlines.analyse(samples, float(deadline), float(period) if period else None, task)
    check = deadlines.cross_check(report, summary)
    qcheck = _qbridged_check(s, report, summary)

    run_id = runs.new_run_id(label or posixpath.basename(remote))
    meta = {"run_id": run_id, "label": label, "program": remote, "binary_sha256": sha,
            "args": ["-d", str(seconds), "-o", csv_remote, *(args or [])], "seconds": seconds,
            "started": started, "target": s.target.resolve_host(),
            "program_summary_fields": sorted((summary or {}).keys())}
    path = runs.save(run_id, meta, summary, csv_text,
                     {**report, "cross_check": check, "qbridged_check": qcheck})
    return {"run_id": run_id, "saved_to": str(path), "binary_sha256": sha,
            "summary": report["summary"], "cross_check": check, "qbridged_check": qcheck,
            "report": {k: v for k, v in report.items() if k != "summary"}}


def _qbridged_check(s: Session, report: dict, summary: dict | None) -> dict:
    """If the workload reported to qbridged, compare qbridged's numbers with ours."""
    info = (summary or {}).get("qbridged") or {}
    if info.get("status") != "registered":
        return {"checked": False,
                "reason": f"the workload did not report to qbridged ({info.get('status', 'unknown')})"}
    r = s.target.run(["cat", qbridged.DEADLINES_PATH])
    if not r.ok:
        return {"checked": False, "reason": f"cannot read {qbridged.DEADLINES_PATH}"}
    tasks = qbridged.parse_deadlines(r.stdout).get("tasks", [])
    task = next((t for t in tasks if t["slot"] == info.get("slot")), None)
    if task is None:
        return {"checked": False, "reason": f"slot {info.get('slot')} not found in qbridged"}
    return qbridged.cross_check(report, task)


def diagnose(s: Session, program: str | None = None, seconds: int = 6, snapshots: int = 40,
             run_id: str | None = None, args: list[str] | None = None) -> dict:
    """Snapshot thread states while a workload runs and apply the analysis rules.

    With `program`, it starts the workload (background, fixed length), takes
    up to `snapshots` /dev/qbridge/threads snapshots while it runs, then reads
    the run's samples so the rule also has deadline evidence from the same run.
    Without `program`, it snapshots whatever is running now. `run_id` adds the
    deadline report of a saved run instead.
    """
    seconds = max(3, min(int(seconds), 60))
    snapshots = max(1, min(int(snapshots), 200))
    csv_remote = None
    log = None
    if program:
        s.policy.require_write("diagnose (starts the workload)")
        remote = s.policy.deploy_path(program)
        csv_remote = remote + ".diagnose.csv"
        started = run(s, program, ["-d", str(seconds), "-o", csv_remote, *(args or [])],
                      timeout=seconds + 30, background=True)
        log = started["log"]
        time.sleep(1.0)   # let the workload reach its steady state

    snaps: list[list[dict]] = []
    deadline_at = time.monotonic() + max(1.0, seconds - 2.0)
    while len(snaps) < snapshots and time.monotonic() < deadline_at:
        r = s.target.run(["cat", qbridged.THREADS_PATH])
        if not r.ok:
            raise TargetError(f"cannot read {qbridged.THREADS_PATH}: is qbridged running?")
        snaps.append(json.loads(r.stdout)["threads"])

    report = None
    source = None
    if run_id:
        report = runs.load(run_id)["report"]
        source = f"saved run {run_id}"
    elif program:
        # Wait for the workload to finish, then analyse its own samples.
        end = time.monotonic() + seconds + 20
        summary = None
        while time.monotonic() < end:
            tail = s.target.run(["tail", "-1", log]).stdout.strip()
            try:
                summary = json.loads(tail)
                break
            except ValueError:
                time.sleep(1.0)
        if summary and summary.get("deadline_us"):
            samples = deadlines.parse_samples_csv(s.target.run(["cat", csv_remote]).stdout)
            if samples:
                report = deadlines.analyse(samples, float(summary["deadline_us"]),
                                           float(summary.get("period_us") or 0) or None,
                                           summary.get("task") or program)
                source = "the same run's samples"

    checks = runs.latest_timing_checks()
    result = analysis.diagnose(snaps, report, checks)
    result["deadline_evidence_from"] = source
    result["workload_args"] = args or []
    result["timing_checks_used"] = [t.get("check_id") for t in checks]
    if report:
        result["deadline_summary"] = report["summary"]
    diag_id = runs.save_diagnosis(program, result, snaps)
    result["diagnosis_id"] = diag_id
    return result


def deadline_stats(s: Session) -> dict:
    """Live deadline statistics of every task registered with qbridged (read-only)."""
    r = s.target.run(["cat", qbridged.DEADLINES_PATH])
    if not r.ok or not r.stdout.strip():
        raise TargetError(f"cannot read {qbridged.DEADLINES_PATH}: is qbridged (with deadline "
                          f"support) running? {r.stderr.strip()}")
    d = qbridged.parse_deadlines(r.stdout)
    for t in d["tasks"]:
        t["summary"] = qbridged.summarise_task(t)
    return d


def timing_check(s: Session, cpu: int = 1, seconds: int = 10) -> dict:
    """Measure the target's timer lateness on one CPU with vmjitter -t, and save it.

    This is the environment's noise floor: how late a high-priority thread's
    1 ms timer fires with nothing else of ours running. Reports include the
    latest saved check. vmjitter (target/tools) must be deployed.
    """
    s.policy.require_write("timing check (runs vmjitter)")
    seconds = max(1, min(int(seconds), 60))
    out = run(s, "vmjitter", ["-t", "-c", str(int(cpu)), "-d", str(seconds)], timeout=seconds + 20)
    if out["exit_code"] != 0:
        raise TargetError(f"vmjitter failed ({out['exit_code']}): {out['stderr'].strip()} "
                          "(deploy target/tools/build/vmjitter first)")
    result = json.loads(out["stdout"].strip().splitlines()[-1])
    result["measured"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    result["target"] = s.target.resolve_host()
    check_id = runs.save_timing_check(result)
    late = result["max_late_us"] / 1000
    result["summary"] = (f"CPU {cpu}: {result['wakeups']} timer wake-ups in {seconds} s; mean "
                         f"lateness {result['mean_late_us'] / 1000:.2f} ms, worst {late:.1f} ms, "
                         f"{result['late_over_1ms']} over 1 ms.")
    result["check_id"] = check_id
    return result


def make_report(s: Session, experiment_id: str, diagnosis_id: str | None = None,
                out: str | None = None) -> dict:
    """Write the HTML report for an experiment, from saved data only."""
    from . import experiments, report
    e = experiments.load(experiment_id)
    diag = None
    if diagnosis_id:
        diag = runs.load_diagnosis(diagnosis_id)
    elif (e.get("proposal") or {}).get("diagnosis_id"):
        diag = runs.load_diagnosis(e["proposal"]["diagnosis_id"])
    else:
        diag = runs.latest_diagnosis(e["program"])

    def load_run(run_id: str) -> dict:
        saved = runs.load(run_id)
        return {"report": saved["report"], "samples": runs.samples_text(run_id),
                "summary": saved.get("summary")}

    html_text = report.build(e, load_run, diag, runs.latest_timing_checks())
    path = Path(out) if out else runs.home() / "reports" / f"{experiment_id}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_text, encoding="utf-8", newline="\n")
    return {"report": str(path), "bytes": len(html_text.encode()),
            "diagnosis_used": (diag or {}).get("diagnosis_id"),
            "timing_checks_used": [t["check_id"] for t in runs.latest_timing_checks()]}


def audit_log(s: Session, limit: int = 30, verify: bool = True) -> dict:
    """Recent audit entries, a count by outcome, and (optionally) a chain check."""
    entries = audit.read(max(1, min(int(limit), 500)))
    out = {"log": str(audit.log_path()), "recent": audit.summary(entries), "entries": entries}
    if verify:
        out["verify"] = audit.verify()
    return out


def list_runs(s: Session, limit: int = 20) -> dict:
    return {"runs": runs.list_runs(limit), "folder": str(runs.runs_dir())}


def get_run(s: Session, run_id: str) -> dict:
    return runs.load(run_id)


def events(s: Session, wait: int = 10, since: int | None = None,
           include_noise: bool = False) -> dict:
    """Process deaths and core dumps pushed by qbridged.

    Reads /dev/qbridge/events for up to `wait` seconds. The read blocks in
    qbridged until something happens, so this returns recent history plus
    anything that occurs while waiting. Pass the returned last_seq as `since`
    next time to get only newer events.
    """
    wait = max(1, min(int(wait), 120))
    r = s.target.run(["timeout", str(wait), "cat", qbridged.EVENTS_PATH], timeout=wait + 15)
    if r.returncode not in (0, TIMEOUT_EXIT):
        raise TargetError(f"cannot read {qbridged.EVENTS_PATH} (is qbridged running?): "
                          f"{r.stderr.strip()}")
    all_events = qbridged.parse_events(r.stdout)
    kept, hidden = qbridged.filter_events(all_events, since, include_noise)
    last = max((e.get("seq", 0) for e in all_events), default=since or 0)
    return {"events": kept, "last_seq": last, "hidden_noise": hidden, "waited_s": wait}


def crashes(s: Session) -> dict:
    return {"core_dirs": crash.CORE_DIRS, "cores": crash.list_cores(s.target)}


def crash_report(s: Session, core: str | None = None, binary: str | None = None) -> dict:
    # Reads the core and binary from the target; changes nothing there.
    return crash.analyse(s.target, core=core, binary=binary)
