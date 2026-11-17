"""Build QNX code on the host with qcc / make and return structured diagnostics."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from . import qnxenv

# gcc-style diagnostic, as printed by qcc (QNX's gcc driver):
#   broken.c:9:5: error: expected ',' or ';' before 'printf'
#   broken.c:8:9: warning: unused variable 'chid' [-Wunused-variable]
_DIAG_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)? "
    r"(?P<sev>fatal error|error|warning|note): "
    r"(?P<msg>.*?)(?: \[(?P<flag>-W[^\]]+)\])?$"
)
# Linker error from the QNX ld:
#   linkfail.c:(.text+0x5): undefined reference to `not_defined_anywhere'
_LINK_RE = re.compile(r"^(?P<file>.+?):\((?P<section>\.[^)]*)\): (?P<msg>.*)$")
_SYMBOL_RE = re.compile(r"[`'](?P<sym>[^`']+)'")

DEFAULT_QCC_TARGET = "gcc_ntox86_64"
LOG_TAIL_LINES = 80


@dataclass
class Diagnostic:
    severity: str          # error | warning | note
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    flag: str | None = None
    symbol: str | None = None
    kind: str = "compile"  # compile | link

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def parse_diagnostics(text: str) -> list[Diagnostic]:
    diags: list[Diagnostic] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _DIAG_RE.match(line)
        if m:
            sev = "error" if m["sev"] == "fatal error" else m["sev"]
            diags.append(Diagnostic(
                severity=sev, message=m["msg"], file=m["file"], line=int(m["line"]),
                column=int(m["col"]) if m["col"] else None, flag=m["flag"],
            ))
            continue
        m = _LINK_RE.match(line)
        if m:
            sym = _SYMBOL_RE.search(m["msg"])
            diags.append(Diagnostic(
                severity="error", message=m["msg"], file=m["file"],
                symbol=sym["sym"] if sym else None, kind="link",
            ))
    return diags


@dataclass
class BuildResult:
    ok: bool
    returncode: int
    command: list[str]
    cwd: str
    errors: list[dict]
    warnings: list[dict]
    log_tail: str

    def to_dict(self) -> dict:
        return asdict(self)


def build(path: str | Path, make_target: str | None = None, output: str | None = None,
          extra_flags: list[str] | None = None, timeout: float = 600) -> BuildResult:
    """Build a directory with make, or a single .c/.cpp file with qcc.

    - directory with a Makefile: runs `make [make_target]` there
    - single source file: runs `qcc -Vgcc_ntox86_64 -g -Wall -o <output> <file>`
    """
    path = Path(path).resolve()
    if path.is_dir():
        if not any((path / n).exists() for n in ("Makefile", "makefile", "GNUmakefile")):
            raise FileNotFoundError(f"no Makefile in {path}")
        cmd = ["make"] + ([make_target] if make_target else [])
        cwd = path
    elif path.is_file():
        variant = DEFAULT_QCC_TARGET + ("_cxx" if path.suffix in (".cpp", ".cc", ".cxx") else "")
        out = output or str(path.with_suffix(""))
        cmd = ["qcc", f"-V{variant}", "-g", "-Wall", *(extra_flags or []), "-o", out, path.name]
        cwd = path.parent
    else:
        raise FileNotFoundError(f"{path} does not exist")

    env = qnxenv.load()
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                           errors="replace", timeout=timeout, shell=False,
                           executable=_which(cmd[0], env))
        rc, log = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        rc, log = -1, f"build timed out after {timeout}s\n{e.stdout or ''}{e.stderr or ''}"
    diags = parse_diagnostics(log)
    errors = [d.to_dict() for d in diags if d.severity == "error"]
    warnings = [d.to_dict() for d in diags if d.severity == "warning"]
    tail = "\n".join(log.splitlines()[-LOG_TAIL_LINES:])
    return BuildResult(rc == 0, rc, cmd, str(cwd), errors, warnings, tail)


def _which(exe: str, env: dict[str, str]) -> str | None:
    """Find exe on the QNX environment's PATH (subprocess uses the parent's PATH)."""
    path = next((v for k, v in env.items() if k.upper() == "PATH"), None)
    return shutil.which(exe, path=path)
