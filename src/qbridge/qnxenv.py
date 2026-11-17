"""Load the QNX SDP environment (QNX_HOST, QNX_TARGET, PATH) for host builds.

The SDP sets these up with qnxsdp-env.bat (Windows) or qnxsdp-env.sh. We run
that script once, capture the resulting environment and reuse it.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path


class QnxEnvError(RuntimeError):
    pass


def default_sdp() -> Path:
    if os.environ.get("QNX_SDP"):
        return Path(os.environ["QNX_SDP"])
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "qnx800"


def _parse_env_dump(text: str, sep: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.split(sep):
        line = line.strip("\r\n")
        if "=" in line and not line.startswith("="):
            k, v = line.split("=", 1)
            env[k] = v
    return env


@lru_cache(maxsize=4)
def load(sdp: Path | None = None) -> dict[str, str]:
    """Return a full environment dict with the QNX SDP set up."""
    sdp = Path(sdp) if sdp else default_sdp()
    if os.name == "nt":
        bat = sdp / "qnxsdp-env.bat"
        if not bat.exists():
            raise QnxEnvError(f"{bat} not found; set QNX_SDP to your SDP folder")
        # One string, not a list: see target.getip for why.
        cmd = f'cmd /s /c "call "{bat}" >nul && set"'
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        env = _parse_env_dump(p.stdout, "\n")
    else:
        sh = sdp / "qnxsdp-env.sh"
        if not sh.exists():
            raise QnxEnvError(f"{sh} not found; set QNX_SDP to your SDP folder")
        p = subprocess.run(["bash", "-c", f'source "{sh}" >/dev/null && env -0'],
                           capture_output=True, text=True, errors="replace")
        env = _parse_env_dump(p.stdout, "\0")
    if p.returncode != 0 or "QNX_HOST" not in {k.upper() for k in env}:
        raise QnxEnvError(f"could not load the QNX environment from {sdp}: {p.stderr.strip()}")
    return env
