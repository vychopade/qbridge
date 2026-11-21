"""Append-only audit log of everything qbridge is asked to do.

Every tool call, from the CLI or the MCP server, is recorded after it runs:
what was asked (tool and arguments), whether the tool can change the target,
the outcome ("ok", "refused" by a guard rail, or "error"), the target
address, and a few key result fields (run id, remote path, signal, verdict,
approval). Approvals of experiment changes are recorded too.

The log is ~/.qbridge/audit.jsonl, one JSON object per line. Each entry
holds the sha256 of the previous entry and its own sha256, so a change or
deletion of an earlier entry is detectable with `qbridge audit --verify`.
This is tamper-evident, not tamper-proof: someone who can write the file
can rewrite the whole chain. It shows what qbridge did; it is not a
security boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import runs

SESSION = uuid.uuid4().hex[:12]     # one id per qbridge process
GENESIS = "0" * 64
MAX_STR = 2000

# Result fields worth keeping in the log (small, identifying).
_KEEP = ("run_id", "experiment_id", "diagnosis_id", "check_id", "remote", "bytes", "signal",
         "verdict", "state", "approved", "exit_code", "timed_out", "timeout_pid", "binary_sha256",
         "target_binary_matches_baseline", "report", "background", "misses", "diff_sha256")


def log_path() -> Path:
    return runs.home() / "audit.jsonl"


def _clip(v):
    if isinstance(v, str):
        return v if len(v) <= MAX_STR else v[:MAX_STR] + f"... [{len(v) - MAX_STR} more chars]"
    if isinstance(v, list):
        return [_clip(x) for x in v[:100]]
    if isinstance(v, dict):
        return {k: _clip(x) for k, x in list(v.items())[:50]}
    return v


def _details(result) -> dict:
    if not isinstance(result, dict):
        return {}
    out = {k: result[k] for k in _KEEP if k in result}
    if "processes" in result and isinstance(result["processes"], list):
        out["pids"] = [p.get("pid") for p in result["processes"] if isinstance(p, dict)][:20]
    rep = result.get("report")
    if isinstance(rep, dict):
        out["misses"] = rep.get("misses")
        out.pop("report", None)
    return _clip(out)


def _hash(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def _locked():
    """Serialise writers (CLI and MCP server may run at the same time)."""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path.with_suffix(".lock"), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock.close()


def _last_entry(path: Path) -> dict | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        chunk = b""
        while pos > 0 and chunk.count(b"\n") < 2:
            step = min(4096, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step) + chunk
    lines = [l for l in chunk.splitlines() if l.strip()]
    return json.loads(lines[-1]) if lines else None


def record(interface: str, tool: str, args: dict | None, changes_target: bool, outcome: str,
           error: str | None = None, result=None, target: str | None = None) -> dict:
    """Append one entry. Never raises: a broken log must not break the tool,
    but a failure to write is reported on stderr."""
    try:
        with _locked():
            path = log_path()
            last = _last_entry(path)
            entry = {
                "seq": (last["seq"] + 1) if last else 1,
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "session": SESSION,
                "interface": interface,
                "tool": tool,
                "args": _clip(args or {}),
                "changes_target": bool(changes_target),
                "outcome": outcome,
                "target": target,
                "prev": last["hash"] if last else GENESIS,
            }
            if error:
                entry["error"] = _clip(str(error))
            d = _details(result)
            if d:
                entry["details"] = d
            entry["hash"] = _hash(entry)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
            return entry
    except Exception as e:  # pragma: no cover - reported, never raised
        import sys
        print(f"qbridge: could not write the audit log: {e}", file=sys.stderr)
        return {}


def read(limit: int = 50) -> list[dict]:
    path = log_path()
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines[-limit:] if l.strip()]


def verify() -> dict:
    """Check every entry's hash and its link to the previous entry."""
    path = log_path()
    if not path.is_file():
        return {"entries": 0, "intact": True, "problems": []}
    prev, seq, problems, n = GENESIS, 0, [], 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        n += 1
        try:
            e = json.loads(line)
        except ValueError:
            problems.append(f"line {lineno}: not valid JSON")
            continue
        if e.get("prev") != prev:
            problems.append(f"line {lineno} (seq {e.get('seq')}): does not link to the entry before it")
        if e.get("hash") != _hash(e):
            problems.append(f"line {lineno} (seq {e.get('seq')}): contents changed after it was written")
        if e.get("seq") != seq + 1:
            problems.append(f"line {lineno}: sequence jumps from {seq} to {e.get('seq')}")
        prev, seq = e.get("hash"), e.get("seq", seq + 1)
    return {"entries": n, "intact": not problems, "problems": problems[:20],
            "note": "Tamper-evident, not tamper-proof: rewriting the whole file is not detectable."}


def summary(entries: list[dict]) -> dict:
    by = {"ok": 0, "refused": 0, "error": 0}
    for e in entries:
        by[e.get("outcome", "error")] = by.get(e.get("outcome", "error"), 0) + 1
    return {"total": len(entries), "changed_target": sum(1 for e in entries if e.get("changes_target")
                                                         and e.get("outcome") == "ok"),
            **by}
