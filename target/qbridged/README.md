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
| `/dev/qbridge/deadlines` | Deadline statistics for every task that registered: period, deadline, cycles, misses, and max / mean / stddev / p50 / p99 of response, backlog, scheduling latency, lock wait and CPU time |
| `/dev/qbridge/samples` | The most recent cycles of each registered task (up to 4096), as CSV |
| `/dev/qbridge/events` | Process deaths and new core dumps as JSON lines. A read returns what this reader has not seen yet, starting with the recent history; when there is nothing new it **blocks** until something happens |

Each `open()` of the first five takes one consistent snapshot, and reads return slices of it, so `cat` and partial reads both work.

## Tasks report their deadlines by message passing

A periodic task includes [qbridge_task.h](qbridge_task.h) (header only):

```c
struct qbt_task qbt;
qbt_register(&qbt, "A-control", 10000 /* period us */, 10000 /* deadline us */);  /* once */
for (;;) {
    /* ... one cycle, timed by the task itself ... */
    qbt_report(&qbt, backlog_ns, latency_ns, lock_wait_ns, exec_ns, response_ns);  /* never blocks */
}
```

- **Register once, before the real-time loop.** `qbt_register()` opens `/dev/qbridge/deadlines` and sends `devctl(DCMD_QBT_REGISTER)`, a `MsgSend()` underneath. qbridged's `qbd_devctl()` ([qb_deadlines.c](qb_deadlines.c)) takes a slot, records the kernel-supplied `ctp->info.pid` and `ctp->info.scoid`, and replies with the slot and a pulse code allocated with `pulse_attach()`.
- **Report every cycle with pulses.** `qbt_report()` sends five `MsgSendPulse()`s on the same file descriptor (on QNX a file descriptor is a connection id), each carrying slot, field and microseconds. The response field is last and completes the cycle. Pulses are queued by the kernel, so the task never waits for qbridged, and they are sent at qbridged's own low priority so delivering them never runs qbridged above the task.
- **Trust.** `qbd_on_pulse()` accepts a pulse only if its `scoid` is the connection that registered that slot.
- **Memory.** All statistics live in a static array (16 tasks, 4096 recent cycles each), so nothing is allocated when a sample arrives. Memory is allocated only when someone reads a file.
- **Close ordering.** A task's `close()` is a message at the task's high priority, so it overtakes the task's last low-priority pulses in qbridged's receive queue. Without handling this, the last cycle was lost (seen on the target: 998 of 999 cycles, 4 rejected pulses). qbridged now keeps accepting pulses from that connection for 2 s after the close, and 999 of 999 arrive.

**Verified on the target:** with rtdemo reporting, `/dev/qbridge/deadlines` and rtdemo's own measurements agreed for both the faulty and the fixed build. Cycles and misses matched exactly (999/296 and 999/0), and worst and 99th-percentile figures matched to the microsecond for all five measures. Means were within 0.2 µs, from rounding to whole microseconds. The per-cycle samples in `/dev/qbridge/samples` match rtdemo's CSV row by row. `qbridge measure` repeats this cross-check on every run.

## Events are pushed, not polled

```
procnto ──PROCESS_CREATE / PROCESS_DEATH pulse──┐
dumper  ──"core written" pulse──────────────────┤
                                                ▼
                                   qbridged channel ──► event ring (256)
                                                │
             cat /dev/qbridge/events ◄── held read() answered with MsgReply()
```

- `setup_events()` creates a side-channel connection to qbridged's own channel (`message_connect`), allocates pulse codes (`pulse_attach`), and builds pulse sigevents marked updateable so the sender can put the pid in the pulse value. Each sigevent is registered with `MsgRegisterEvent()` before handing it over: to procnto with `procmgr_event_notify_add(PROCMGR_EVENT_PROCESS_DEATH | _CREATE)`, and to dumper with `devctl(DCMD_DUMPER_NOTIFYEVENT)` on `/proc/dumper`.
- `on_pulse()` handles them. For a core it asks dumper for the file with `DCMD_DUMPER_GETPATH`. A pid→path cache (`cache_put`, `cache_get`) remembers names, because by the time a death pulse arrives the process is gone from `/proc`.
- `events_read()` replies straight away if there are unseen events; otherwise it stores the client's `rcvid` and returns `_RESMGR_NOREPLY`. The client stays REPLY-blocked on qbridged until `add_event()` → `wake_waiters()` answers it with `MsgReply()`. If the client gives up (a signal, `timeout`), the kernel sends an unblock pulse and `qb_unblock()` releases it with `MsgError(EINTR)`.

Known limit: a program started with fork+exec that lives only a few milliseconds may be reported under its parent's name (usually `sh`). QNX sends no pulse on exec, and the process is already a zombie when its death pulse arrives. Programs that crash are always named correctly, because dumper's pulse arrives while the process still exists.

## What it uses (QNX-specific code to look at)

In [qbridged.c](qbridged.c) (deadline reporting is in [qb_deadlines.c](qb_deadlines.c) and [qbridge_task.h](qbridge_task.h), described below):

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

## Start it at boot

qbridge adds qbridged to a `mkqnximage` image with two snippet files in [target/image/snippets](../image/snippets): `system_files.qbridge` puts the binary at `/system/bin/qbridged`, and `post_start.qbridge` starts it from the image's `post_startup.sh`.

```
qbridge build target\qbridged
qbridge image install
cd %USERPROFILE%\qnx-target
mkqnximage --stop
mkqnximage --build --run
```

`qbridge image install` copies the binary and both snippets into the image folder (`QBRIDGE_IMAGE_DIR`, default `%USERPROFILE%\qnx-target`). Rebuilding resets the target's `/data` partition, and the target may come back with a new address; qbridge finds it with `mkqnximage --getip`.

## Checked against pidin

On a QNX 8.0 x86_64 VirtualBox target, a back-to-back snapshot of `/dev/qbridge/threads` and `pidin` listed the same 115 threads with identical priorities. The only differences were the short-lived `cat`/`pidin` processes themselves and threads that changed state between the two snapshots. qbridged also reports full thread names, where pidin truncates long ones.

## Tested on the target

- A reader held on `/dev/qbridge/events` received the `core` and `death` events for the crashing `examples/crash_null` program as they happened.
- `timeout 3 cat /dev/qbridge/events` with no new events returns after 3 seconds (the unblock path works).
- After a rebuild of the image, qbridged starts at boot and is already recording events.
