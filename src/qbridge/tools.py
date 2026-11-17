"""The qbridge tools. The CLI and the MCP server both call these.

Each tool returns a JSON-friendly dict. Tools that change the target check
the Policy first.
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import build as build_mod
from . import crash
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


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return f"[... {len(text) - MAX_OUTPUT} characters cut ...]\n" + text[-MAX_OUTPUT:]


def info(s: Session) -> dict:
    r = s.target.run(["sh", "-c", "uname -a; pidin info"])
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
    r = s.target.run(["sh", "-c", f"chmod +x {shlex.quote(tmp)} && mv -f {shlex.quote(tmp)} {shlex.quote(remote)}"])
    if not r.ok:
        raise TargetError(f"installing {remote} failed: {r.stderr.strip()}")
    return {"local": str(local), "remote": remote, "bytes": local.stat().st_size}


MAX_BACKGROUND_S = 3600
KILL_SIGNALS = {"TERM", "INT", "HUP", "KILL", "QUIT", "ABRT", "USR1", "USR2"}


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
    cmd = " ".join(shlex.quote(a) for a in ["timeout", str(t), remote, *args])
    r = s.target.run(["sh", "-c", f"{cmd} < /dev/null > {shlex.quote(log)} 2>&1 & echo $!"])
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
    script = ('for p in /proc/[0-9]*; do printf "%s|" "${p#/proc/}"; '
              'cat "$p/exefile" 2>/dev/null; echo; done')
    r = s.target.run(["sh", "-c", script])
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
        lr = s.target.run(["sh", "-c", f"sleep 0.3; tail -c 4000 {shlex.quote(path + '.log')} 2>/dev/null"])
        if lr.stdout.strip():
            logs[path + ".log"] = lr.stdout
    if logs:
        out["logs"] = logs
    if sig in ("ABRT", "QUIT"):
        out["hint"] = "dumper should have written a core file; use qnx_crash to see where it was stuck."
    return out


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
