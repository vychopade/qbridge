# rtdemo: a control task that misses its deadline

An educational, simulated real-time workload for qbridge RT. It is not a model of any real product and makes no safety claims.

## What runs

Three threads, pinned to one CPU (CPU 1 by default). The `-s msgpass` scenario adds a server process, described [below](#third-scenario-priority-inversion-through-message-passing-qnx-specific).

| Thread | Priority (FIFO) | Does |
|---|---|---|
| A `A-control` | 30 | Every 10 ms (deadline 10 ms): takes the shared lock for 0.2 ms of work, then 0.8 ms more work. Measures itself. |
| B `B-worker` | 20 | Every 100 ms: a 40 ms CPU-heavy burst, starting 3 ms into the window. |
| C `C-logger` | 10 | Every 20 ms: takes the shared lock and holds it for 3 ms of work, starting 2 ms into the window. |

**Planted fault:** the shared lock is created with `PTHREAD_PRIO_NONE` (`LOCK_PROTOCOL` near the top of [rtdemo.c](rtdemo.c)). On QNX 8.0 a mutex uses `PTHREAD_PRIO_INHERIT` unless told otherwise (see `pthread_mutexattr_init()` in the SDP docs). Without inheritance, B's burst preempts C while C holds the lock, and A then waits behind B. That is priority inversion. Restoring `PTHREAD_PRIO_INHERIT` is the fix: C then runs at A's priority as soon as A waits.

The workload is fixed, with the same phases every run, so before and after runs are comparable.

## Second scenario: CPU overload with the wrong priorities

```
rtdemo -s overload -a 15
```

There's no lock in this scenario: A does the same 1 ms of work without the shared lock, and C does not run. `-a` sets A's priority. At 15, A is **below** B (20), so every time B's 40 ms burst runs, A is ready but cannot get the CPU, and it misses its deadlines. The fix is a scheduling change, not a code change: `-a 30` puts A above B.

## Third scenario: priority inversion through message passing (QNX-specific)

```
rtdemo -s msgpass
```

No mutex is involved. A asks a separate server process, S (`S-server`, priority 10, same CPU), for a result every cycle with `MsgSend`; S does 4 ms of work per request and replies. On QNX a server thread that receives a message normally runs at the sending client's priority, so S would serve A at 30. **Planted fault:** S's channel is created with `_NTO_CHF_FIXED_PRIORITY` (`SERVER_CHANNEL_FLAGS` near the top of [rtdemo.c](rtdemo.c)), which turns that off. S then serves A at its own priority 10, B's burst (20) preempts S in the middle of A's request, and A stays REPLY-blocked for the whole burst. The fix is one line: create the channel without the flag. S is forked before any other thread starts and exits when A's connection goes away.

The results measured on the reference target are in the [main README](../../README.md#result-on-the-reference-target) and [docs/example-report-msgpass.html](../../docs/example-report-msgpass.html); the record is in `tests/fixtures/experiment_live_msgpass/`.

## How qbridge tells the three faults apart

- **Priority inversion on a mutex** (the default scenario): A is MUTEX-blocked on a lock held by a lower-priority thread.
- **Priority inversion through message passing** (`-s msgpass`): A is SEND- or REPLY-blocked on a server process running below A's priority, while a middle-priority thread has the server's CPU.
- **CPU preemption** (`-s overload`): A is READY while a higher-priority thread is RUNNING on its CPU, the misses start with waiting for the CPU, and lock wait is zero.

Each rule was checked on recorded target data from the other scenarios and fires only on its own fault (`tests/test_scenarios.py`, `tests/test_msgpass.py`).

Measured on the reference target (Nov 22, 2026), 3 runs of 10 s each way. The change is to the program's arguments only (same binary, checked by sha256), approved by a person at the prompt:

| `-s overload` | `-a 15` (fault) | `-a 30` (fix) |
|---|---|---|
| Misses (of 999) | 301, 309, 305 | 0, 1, 0 |
| Worst response | 36.1–46.2 ms | 5.0–10.6 ms |
| 99th percentile | 35.5–36.1 ms | 3.0 ms |

The verdict was "improved": every run after the change beat every run before it. The single miss after the change was a 10.6 ms cycle. On that day CPU 1's timer lateness reached 28.4 ms in the timing check (it was 2–5 ms on Nov 17), so a few misses from the VM are expected. The report is [docs/example-report-overload.html](../../docs/example-report-overload.html), and the record is in `tests/fixtures/experiment_live_overload/`.

## What A measures, per cycle

| Field | Meaning |
|---|---|
| `release_us` | When the cycle was due (since start) |
| `backlog_us` | How far the previous cycle ran past this release (0 if it finished in time) |
| `latency_us` | Scheduling latency: wake-up time minus the moment A could first have started |
| `lock_wait_us` | Time spent waiting for the shared lock |
| `server_wait_us` | `-s msgpass` only: time in `MsgSend`, from send to reply (the server's own work plus any wait for it) |
| `exec_us` | CPU time A actually used (`CLOCK_THREAD_CPUTIME_ID`) |
| `response_us` | Finish time minus release; compared with the 10 ms deadline |

Execution time, scheduling latency and lock wait are kept separate so a miss can be attributed to the right cause. Samples are stored in memory allocated before the run and written to a CSV afterwards. One JSON summary line goes to stdout.

```
rtdemo [-d seconds] [-o samples.csv] [-c cpu] [-n] [-s inversion|overload|msgpass] [-a A-priority]
```

If qbridged is running, A also registers with it and reports every cycle by message passing ([qbridge_task.h](../../target/qbridged/qbridge_task.h)). This happens after the cycle's own timestamps, so it isn't counted in A's response, and it never blocks. `-n` turns it off. The summary line says whether A registered, its slot, and how many pulses the kernel refused (0 in every run so far).

Absolute numbers vary between VM boots. After one image rebuild, the faulty build missed 384 of 999 instead of about 300, and the fixed build's mean response rose from 1.7 to 2.6 ms. Compare runs taken in the same session.

## Measured on the reference target

QNX 8.0 x86_64 under VirtualBox 7.2 on Windows 11, 2 virtual CPUs, three 10-second runs each (999 cycles per run):

| Lock protocol | Misses | Worst response | 99th percentile | Worst lock wait |
|---|---|---|---|---|
| `PTHREAD_PRIO_NONE` (fault) | 301, 310, 301 | 37.2–37.4 ms | 36.6–37.0 ms | 35.9–36.1 ms |
| `PTHREAD_PRIO_INHERIT` (fix) | 0, 0, 0 | 4.1–5.5 ms | 3.8–4.1 ms | 2.8–2.9 ms |

The raw summaries and per-cycle samples are in `tests/fixtures/rtdemo*_summary.json` and `rtdemo*_samples.csv`.

## Timing on a virtual machine (read this)

Before the demo was moved to CPU 1, the "fixed" build still missed about a quarter of its deadlines. A per-thread timeline showed all three threads oversleeping their timers together, and [`target/tools/vmjitter`](../../target/tools/vmjitter.c) confirmed the cause:

| Check (10 s, priority 50, nothing else running) | CPU 0 | CPU 1 |
|---|---|---|
| Timer wake-ups at 1 ms period (about 10,000 expected) | 529–540 | 9,497–9,658 |
| Mean timer lateness | about 18 ms | about 0.33 ms |
| Worst timer lateness | about 55 ms | 1.4–4.9 ms |
| Worst gap while spinning | 0.8–4.1 ms | 0.8–4.6 ms |

On this VM, timers on virtual CPU 0 fire tens of milliseconds late, so rtdemo runs on CPU 1. Even CPU 1 shows occasional multi-millisecond delays that have nothing to do with the program. These numbers come from finite runs on one virtual machine. They show the effect of the fault clearly, but they do **not** establish a worst-case response time, and real hardware will behave differently. Run `vmjitter -t -c <cpu>` on your own target before trusting any timing result.
