# qbridge

AI assistants can write QNX code but can't see the target. qbridge gives them eyes and hands on QNX, with guard rails.

qbridge lets any MCP client (or you, from the command line) build, deploy, inspect and debug software on a QNX 8.0 target.

> Status: early work for the QNX Global Hackathon. The host CLI can list threads (`ps`) and read logs (`logs`) today. The MCP server, build/deploy/crash tools and the `qbridged` resource manager are in progress.

## Requirements

- QNX SDP 8.0 on the host (tested on Windows 11)
- A QNX 8.0 x86_64 target, e.g. a VirtualBox image made with `mkqnximage`
- Python 3.10+

## Quick start

1. Make an ssh key for the target and build the image with it (from a Command Prompt after running `qnxsdp-env.bat`):

   ```
   ssh-keygen -t ed25519 -f %USERPROFILE%\.ssh\qnx_target_ed25519 -N ""
   mkdir %USERPROFILE%\qnx-target && cd %USERPROFILE%\qnx-target
   mkqnximage --type=vbox --arch=x86_64 --ssh-ident=%USERPROFILE%/.ssh/qnx_target_ed25519.pub --run
   ```

2. Install qbridge and point it at the target:

   ```
   pip install -e .[dev]
   set QBRIDGE_HOST=192.168.56.103
   qbridge ps
   qbridge logs --process io_sock --severity warning
   ```

   Without `QBRIDGE_HOST`, qbridge asks `mkqnximage --getip` in `QBRIDGE_IMAGE_DIR` (default `%USERPROFILE%\qnx-target`).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `QBRIDGE_HOST` | from `mkqnximage --getip` | Target address |
| `QBRIDGE_USER` | `root` | ssh user |
| `QBRIDGE_KEY` | `~/.ssh/qnx_target_ed25519` | ssh private key |
| `QBRIDGE_IMAGE_DIR` | `~/qnx-target` | mkqnximage image folder |
| `QNX_SDP` | `~/qnx800` | QNX SDP install folder |

## Notes from the real target

- Windows OpenSSH fails against the QNX 8.0 sshd with "Corrupted MAC on input" unless the MAC is forced to `hmac-sha2-256`; qbridge does this for you.
- `dumper` runs as `dumper -U 30:30 -d /data/var/dumper -M 1000 -D cores`, so core files land in `/data/var/dumper`.

## Tests

Tests run against output recorded from a real target (`tests/fixtures/`), so no target is needed:

```
pytest
```

## Licence

MIT, see [LICENSE](LICENSE).
