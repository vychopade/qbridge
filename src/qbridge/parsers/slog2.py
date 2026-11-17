"""Parse slog2info output.

qbridge runs slog2info with flags that make the output machine-readable
(checked on a QNX 8.0 target):

    slog2info -f -S -t
    -f  timestamps as epoch_seconds.milliseconds
    -S  severity as SLOG2_<LEVEL>
    -t  thread id column

Example line:

    1794886004.126   devb_eide.90123   slog    1   0   SLOG2_WARNING  PCI WARN ...
    timestamp        bufset.pid[..n]   buffer  tid code severity      message

A '*' after the buffer name marks the first line slog2info printed from
that buffer. Buffer set names use '_' where the program name has '-'.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

SLOG2_ARGS = ["slog2info", "-f", "-S", "-t"]

SEVERITIES = ["SHUTDOWN", "CRITICAL", "ERROR", "WARNING", "NOTICE", "INFO", "DEBUG1", "DEBUG2"]

_LINE_RE = re.compile(
    r"^(?P<ts>\d+\.\d+)\s+"
    r"(?P<bufset>\S+)\s+"
    r"(?P<buffer>\S+?)(?P<first>\*?)\s+"
    r"(?P<tid>\d+)\s+"
    r"(?P<code>-?\d+)\s+"
    r"SLOG2_(?P<sev>[A-Z0-9]+)"
    r"(?:  (?P<msg>.*))?$"
)
_BUFSET_RE = re.compile(r"^(?P<name>.+?)\.(?P<pid>\d+)(?:\.\.(?P<idx>\d+))?$")


@dataclass
class LogEntry:
    time: float
    process: str
    pid: int | None
    buffer: str
    tid: int
    code: int
    severity: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def parse(text: str) -> list[LogEntry]:
    entries: list[LogEntry] = []
    for line in text.lstrip("﻿").splitlines():
        m = _LINE_RE.match(line.rstrip("\r"))
        if not m:
            if entries and line.strip():
                # Continuation of a multi-line message.
                entries[-1].message += "\n" + line.rstrip()
            continue
        b = _BUFSET_RE.match(m["bufset"])
        process = b["name"] if b else m["bufset"]
        pid = int(b["pid"]) if b else None
        entries.append(LogEntry(
            time=float(m["ts"]),
            process=process,
            pid=pid,
            buffer=m["buffer"],
            tid=int(m["tid"]),
            code=int(m["code"]),
            severity=m["sev"],
            message=(m["msg"] or "").rstrip(),
        ))
    return entries


def _norm(name: str) -> str:
    return name.replace("-", "_").lower()


def filter_entries(
    entries: list[LogEntry],
    process: str | None = None,
    pid: int | None = None,
    since: float | None = None,
    min_severity: str | None = None,
    contains: str | None = None,
    limit: int | None = None,
) -> list[LogEntry]:
    """Filter log entries. min_severity keeps that level and anything worse."""
    max_rank = None
    if min_severity:
        sev = min_severity.upper().removeprefix("SLOG2_")
        if sev not in SEVERITIES:
            raise ValueError(f"unknown severity {min_severity!r}; use one of {', '.join(SEVERITIES)}")
        max_rank = SEVERITIES.index(sev)
    out = []
    for e in entries:
        if process and _norm(process) not in _norm(e.process):
            continue
        if pid is not None and e.pid != pid:
            continue
        if since is not None and e.time < since:
            continue
        if max_rank is not None:
            rank = SEVERITIES.index(e.severity) if e.severity in SEVERITIES else len(SEVERITIES)
            if rank > max_rank:
                continue
        if contains and contains.lower() not in e.message.lower():
            continue
        out.append(e)
    if limit is not None:
        out = out[-limit:]
    return out
