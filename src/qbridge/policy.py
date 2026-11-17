"""Guard rails. qbridge is read-only unless write access is switched on.

Read-only tools (ps, logs, info, build on the host) always work. Tools that
change the target (deploy, run, and later kill) need allow_write, set with
--allow-write or QBRIDGE_ALLOW_WRITE=1. Deploys are confined to one folder
on the target so a tool call cannot overwrite system files.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass

DEPLOY_ROOT = "/data/qbridge"


class PolicyError(PermissionError):
    pass


@dataclass
class Policy:
    allow_write: bool = False
    deploy_root: str = DEPLOY_ROOT

    @classmethod
    def from_env(cls, allow_write: bool = False) -> "Policy":
        env_flag = os.environ.get("QBRIDGE_ALLOW_WRITE", "").lower() in ("1", "true", "yes")
        return cls(allow_write=allow_write or env_flag,
                   deploy_root=os.environ.get("QBRIDGE_DEPLOY_ROOT", DEPLOY_ROOT))

    def require_write(self, action: str) -> None:
        if not self.allow_write:
            raise PolicyError(
                f"'{action}' changes the target and qbridge is read-only. "
                "Restart qbridge with --allow-write (or QBRIDGE_ALLOW_WRITE=1) to allow it."
            )

    def deploy_path(self, name_or_path: str) -> str:
        """Resolve a target path and make sure it stays inside the deploy root."""
        root = posixpath.normpath(self.deploy_root)
        p = name_or_path if name_or_path.startswith("/") else posixpath.join(root, name_or_path)
        p = posixpath.normpath(p)
        if p != root and not p.startswith(root + "/"):
            raise PolicyError(f"{name_or_path!r} is outside the deploy folder {root}")
        return p
