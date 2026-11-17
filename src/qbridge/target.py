"""Connection to a QNX target over ssh.

All commands that run on the target go through Target.run(), so the safety
layer has one place to hook in later.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Windows OpenSSH fails against the QNX 8.0 sshd with "Corrupted MAC on input"
# unless the MAC is forced. Verified on a QNX 8.0 VirtualBox image, Nov 2026.
SSH_MAC = "hmac-sha2-256"


class TargetError(RuntimeError):
    """The target could not be reached or a command failed to run."""


@dataclass
class Result:
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


@dataclass
class Target:
    host: str | None = None
    user: str = "root"
    key: Path = field(default_factory=lambda: _home() / ".ssh" / "qnx_target_ed25519")
    port: int = 22
    image_dir: Path = field(default_factory=lambda: _home() / "qnx-target")
    sdp: Path = field(default_factory=lambda: _home() / "qnx800")
    connect_timeout: int = 10

    @classmethod
    def from_env(cls) -> "Target":
        t = cls()
        env = os.environ
        if env.get("QBRIDGE_HOST"):
            t.host = env["QBRIDGE_HOST"]
        if env.get("QBRIDGE_USER"):
            t.user = env["QBRIDGE_USER"]
        if env.get("QBRIDGE_KEY"):
            t.key = Path(env["QBRIDGE_KEY"])
        if env.get("QBRIDGE_PORT"):
            t.port = int(env["QBRIDGE_PORT"])
        if env.get("QBRIDGE_IMAGE_DIR"):
            t.image_dir = Path(env["QBRIDGE_IMAGE_DIR"])
        if env.get("QNX_SDP"):
            t.sdp = Path(env["QNX_SDP"])
        return t

    # -- address -----------------------------------------------------------

    def resolve_host(self) -> str:
        """Return the target address, asking mkqnximage if none is set.

        The VirtualBox host-only DHCP server can hand out a new address on
        each boot, so the address is not hard-coded.
        """
        if self.host:
            return self.host
        self.host = getip(self.image_dir, self.sdp)
        return self.host

    # -- ssh ---------------------------------------------------------------

    def _common_opts(self) -> list[str]:
        # Shared by ssh and scp. MACs is set with -o so scp accepts it too.
        return [
            "-i", str(self.key),
            "-o", f"MACs={SSH_MAC}",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
        ]

    def ssh_args(self, remote_command: str) -> list[str]:
        return [
            "ssh", *self._common_opts(), "-p", str(self.port),
            f"{self.user}@{self.resolve_host()}",
            remote_command,
        ]

    def scp_args(self, local: str, remote: str) -> list[str]:
        return [
            "scp", *self._common_opts(), "-P", str(self.port),
            local, f"{self.user}@{self.resolve_host()}:{remote}",
        ]

    def put(self, local: Path, remote: str, timeout: float = 120) -> None:
        """Copy one file to the target."""
        try:
            p = subprocess.run(self.scp_args(str(local), remote), capture_output=True,
                               text=True, errors="replace", timeout=timeout)
        except FileNotFoundError as e:
            raise TargetError("scp was not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise TargetError(f"copying {local} timed out after {timeout}s") from e
        if p.returncode != 0:
            raise TargetError(f"scp {local} -> {remote} failed: {p.stderr.strip()}")

    def run(self, argv: list[str], timeout: float = 60) -> Result:
        """Run a command on the target. argv is quoted for the target shell."""
        remote = " ".join(shlex.quote(a) for a in argv)
        args = self.ssh_args(remote)
        try:
            p = subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        except FileNotFoundError as e:
            raise TargetError("ssh was not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise TargetError(f"timed out after {timeout}s: {remote}") from e
        if p.returncode == 255:
            raise TargetError(f"ssh to {self.host} failed: {p.stderr.strip()}")
        return Result(remote, p.returncode, p.stdout, p.stderr)


def getip(image_dir: Path, sdp: Path, timeout: float = 120) -> str:
    """Ask mkqnximage for the address of the image in image_dir."""
    if os.name == "nt":
        env_bat = sdp / "qnxsdp-env.bat"
        # Passed as one string: list-style quoting turns the inner quotes into
        # \" which cmd.exe does not understand. /s strips only the outer quotes.
        args = f'cmd /s /c "call "{env_bat}" >nul && mkqnximage --getip"'
    else:
        env_sh = sdp / "qnxsdp-env.sh"
        args = ["bash", "-c", f'source "{env_sh}" >/dev/null && mkqnximage --getip']
    try:
        p = subprocess.run(args, cwd=image_dir, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, NotADirectoryError) as e:
        raise TargetError(f"cannot run mkqnximage in {image_dir}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise TargetError("mkqnximage --getip timed out; is the target running?") from e
    ip = p.stdout.strip().splitlines()[-1].strip() if p.stdout.strip() else ""
    if p.returncode != 0 or not ip:
        raise TargetError(f"mkqnximage --getip failed: {(p.stderr or p.stdout).strip()}")
    return ip
