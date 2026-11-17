# qbridged

A QNX resource manager that serves live system state as JSON files under `/dev/qbridge`. It is the QNX-native half of qbridge: the host tools read these files over ssh instead of scraping `pidin` text.

## Files

| Path | Contents |
|---|---|
| `/dev/qbridge/version` | Daemon name, version, pid, core folder |
| `/dev/qbridge/procs` | Every process: pid, parent, full path, thread count, uid, CPU times, private memory |
| `/dev/qbridge/threads` | Every thread: state, priority (current and real), policy, full thread name, last CPU, how long it has been blocked (`blocked_ms`), and what it is blocked on |
| `/dev/qbridge/blocked` | The blocked-on graph: one edge per SEND/REPLY-blocked thread, with the server pid, channel id and duration |
| `/dev/qbridge/crashes` | Core files written by `dumper` |

Each `open()` takes one consistent snapshot, and reads return slices of it, so `cat` and partial reads both work.

## What it uses (QNX-specific code to look at)

All in [qbridged.c](qbridged.c):

- **Resource manager framework.** `main()` attaches one pathname per file with `resmgr_attach()`, using the `iofunc` layer with a custom OCB (`struct qb_ocb`, `qb_ocb_calloc`, `qb_ocb_free`) that holds the per-open snapshot. `qb_read()` is the `_IO_READ` handler, replying with `_RESMGR_NPARTS` and `SETIOV`.
- **procfs devctls.** `walk_threads()` opens `/proc/<pid>/ctl` and uses `DCMD_PROC_INFO` for process data and `DCMD_PROC_TIDSTATUS` to walk threads. The kernel's `debug_thread_t` gives `blocked.connect.pid/chid/coid/scoid` for SEND/REPLY, which is how the blocked-on graph is exact rather than parsed.
- **Thread names.** `thread_name()` uses `ThreadCtlExt(pid, tid, _NTO_TCTL_NAME, ...)` with `new_name_len = -1` to read another process's thread names in full.
- **Daemon.** `procmgr_daemon()` detaches from the starting shell.

## Build and run

From the repo root on the host (after `qnxsdp-env.bat`, or through qbridge):

```
qbridge build target\qbridged
qbridge --allow-write deploy target\qbridged\build\qbridged
qbridge --allow-write run qbridged
```

On the target: `qbridged [-f] [-d core_dir] [-m mount_prefix]`. `-f` stays in the foreground. It must run as root to read other processes' `/proc` entries and attach under `/dev`.

## Checked against pidin

On a QNX 8.0 x86_64 VirtualBox target, a back-to-back snapshot of `/dev/qbridge/threads` and `pidin` listed the same 115 threads with identical priorities. The only differences were the short-lived `cat`/`pidin` processes themselves and threads that changed state between the two snapshots. qbridged also reports full thread names, where pidin truncates long ones.

## Not done yet

- Push events over pulses (process death, new core dump) instead of polling.
- Starting qbridged from the image's startup script via `mkqnximage`.
