"""Changes to the mkqnximage image folder on the host.

mkqnximage reads 'snippet' files from <image>/local/snippets: files named
system_files.* are added to the system partition and post_start.* lines are
appended to the boot script. qbridge ships its own snippets (suffix .qbridge)
in target/image/snippets so qbridged is built into the image and started at
boot.
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = REPO_ROOT / "target" / "image" / "snippets"
QBRIDGED_BUILD = REPO_ROOT / "target" / "qbridged" / "build" / "qbridged"


def install_qbridged(image_dir: Path, binary: Path | None = None) -> dict:
    """Copy qbridged and its snippets into the image folder. Rebuild the image afterwards."""
    image_dir = Path(image_dir)
    if not (image_dir / "local" / "snippets").is_dir():
        raise FileNotFoundError(f"{image_dir} is not a mkqnximage image folder (no local/snippets)")
    binary = Path(binary) if binary else QBRIDGED_BUILD
    if not binary.is_file():
        raise FileNotFoundError(f"{binary} not found; build it first with: qbridge build target/qbridged")

    copied = []
    dest_bin = image_dir / "local" / "qbridge" / "qbridged"
    dest_bin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, dest_bin)
    copied.append(str(dest_bin))
    for snip in sorted(SNIPPETS.glob("*.qbridge")):
        dest = image_dir / "local" / "snippets" / snip.name
        shutil.copy2(snip, dest)
        copied.append(str(dest))
    return {
        "image_dir": str(image_dir),
        "copied": copied,
        "next": "Rebuild and restart the image: mkqnximage --stop, then mkqnximage --build --run "
                "(this resets the target's /data partition).",
    }
