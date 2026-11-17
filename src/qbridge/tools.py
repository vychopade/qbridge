"""The qbridge tools. The CLI and the MCP server both call these.

Each tool returns a JSON-friendly dict. Tools that change the target check
the Policy first.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from . import build as build_mod
from . import crash
from .parsers import pidin, slog2
from .policy import Policy
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


def ps(s: Session, process: str | None = None) -> dict:
    r = s.target.run(pidin.PIDIN_ARGS)
    if not r.ok:
        raise TargetError(f"pidin failed: {r.stderr.strip()}")
    threads = pidin.parse_threads(r.stdout)
    # Build the graph from every thread so edges keep the server's name.
    edges = pidin.blocked_graph(threads)
    cycles = pidin.find_cycles(edges)
    if process:
        want = process.lower()
        threads = [t for t in threads if want in t.name.lower() or str(t.pid) == process]
        pids = {t.pid for t in threads}
        edges = [e for e in edges if e["from_pid"] in pids or e["to_pid"] in pids]
        cycles = [c for c in cycles if pids & set(c)]
    return {"processes": pidin.processes(threads), "blocked_on": edges, "cycles": cycles}


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
    s.target.put(local, remote)
    r = s.target.run(["chmod", "+x", remote])
    if not r.ok:
        raise TargetError(f"chmod failed: {r.stderr.strip()}")
    return {"local": str(local), "remote": remote, "bytes": local.stat().st_size}


def run(s: Session, path: str, args: list[str] | None = None, timeout: int = 30) -> dict:
    s.policy.require_write("run")
    remote = s.policy.deploy_path(path)
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


def crashes(s: Session) -> dict:
    return {"core_dirs": crash.CORE_DIRS, "cores": crash.list_cores(s.target)}


def crash_report(s: Session, core: str | None = None, binary: str | None = None) -> dict:
    # Reads the core and binary from the target; changes nothing there.
    return crash.analyse(s.target, core=core, binary=binary)
