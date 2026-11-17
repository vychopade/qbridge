# rtdemo: a control task that misses its deadline

An educational, simulated real-time workload for qbridge RT. It is not a model of any real product and makes no safety claims.

## What runs

Three threads, pinned to one CPU (CPU 1 by default):

| Thread | Priority (FIFO) | Does |
|---|---|---|
| A `A-control` | 30 | Every 10 ms (deadline 10 ms): takes the shared lock for 0.2 ms of work, then 0.8 ms more work. Measures itself. |
| B `B-worker` | 20 | Every 100 ms: a 40 ms CPU-heavy burst, starting 3 ms into the window. |
| C `C-logger` | 10 | Every 20 ms: takes the shared lock and holds it for 3 ms of work, starting 2 ms into the window. |

**Planted fault:** the shared lock is created with `PTHREAD_PRIO_NONE` (`LOCK_PROTOCOL` near the top of [rtdemo.c](rtdemo.c)). On QNX 8.0 a mutex uses `PTHREAD_PRIO_INHERIT` unless told otherwise (see `pthread_mutexattr_init()` in the SDP docs). Without inheritance, B's burst preempts C while C holds the lock, and A then waits behind B. That is priority inversion. Restoring `PTHREAD_PRIO_INHERIT` is the fix: C then runs at A's priority as soon as A waits.

The workload is fixed, with the same phases every run, so before and after runs are comparable.

## What A measures, per cycle

| Field | Meaning |
|---|---|
| `release_us` | When the cycle was due (since start) |
| `backlog_us` | How far the previous cycle ran past this release (0 if it finished in time) |
| `latency_us` | Scheduling latency: wake-up time minus the moment A could first have started |
| `lock_wait_us` | Time spent waiting for the shared lock |
| `exec_us` | CPU time A actually used (`CLOCK_THREAD_CPUTIME_ID`) |
| `response_us` | Finish time minus release; compared with the 10 ms deadline |

Execution time, scheduling latency and lock wait are kept separate so a miss can be attributed to the right cause. Samples are stored in memory allocated before the run and written to a CSV afterwards. One JSON summary line goes to stdout.

```
rtdemo [-d seconds] [-o samples.csv] [-c cpu] [-n]
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
