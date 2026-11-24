# Recorded kernel traces

Five one-second kernel traces from a QNX 8.0 virtual machine (2 CPUs), recorded with `tracelogger` while [rtdemo](../rtdemo/README.md) ran. Each is the full trace: every process on the target, not just rtdemo, as the raw `.kev` and as `traceprinter` text; qbridge reads both.

Try qbridge's trace analysis on them without a QNX target:

```
qbridge trace --file examples/traces/msgpass.txt.gz
```

It prints JSON and writes an HTML page with a thread-state timeline (the path is in the `html` field). For every periodic thread it also rebuilds each cycle from the trace and reports deadline statistics (period found from the wake-ups, deadline taken as the period unless you pass `--period-ms` and `--deadline-ms` with `--thread`). Add `--perfetto` to also write a file for [ui.perfetto.dev](https://ui.perfetto.dev): a track per thread and per CPU, with priorities, and a marker at each hint. Name threads with `--thread A-control` (or `--thread <pid>/<tid>`), or a whole process with `--process rtdemo`. An AI assistant can do the same with the `qnx_trace_file` MCP tool.

| File | Workload | What the analysis should point at |
|---|---|---|
| `inversion.txt.gz` | `rtdemo -s inversion` | Possible priority inversion: `A-control` (priority 30) blocked on a mutex while `C-logger` (10) is held off its CPU by `B-worker` (20) |
| `msgpass.txt.gz` | `rtdemo -s msgpass` | Possible priority inversion: `A-control` (30) blocked in SEND/REPLY while the server `S-server` (10) is held off its CPU by `B-worker` (20) |
| `msgpass_fixed.txt.gz` | `rtdemo -s msgpass`, fixed | No inversion: the server now runs at its client's priority (30), so `B-worker` waits behind it instead |
| `condvar.txt.gz` | `rtdemo -s condvar` | Possible priority inversion: `A-control` (30) blocked on a condition variable while `P-producer` (10), the thread that signals it, is held off its CPU by `B-worker` (20); A-control's rebuilt cycles miss about 60% of deadlines |
| `overload.txt.gz` | `rtdemo -s overload -a 15` | CPU preemption: `A-control` (15) ready but not running while `B-worker` (20) has its CPU |

These are hints from thread states and priorities. A trace does not record what a blocked thread waits for, so a trace on its own cannot prove which thread held the mutex or answered the message. On a live target, `qbridge diagnose --trace` reads that from the target and checks each finding against a trace from the same run.

Each trace is also here as the raw file `tracelogger` wrote (`<name>.kev.gz`); the `.txt.gz` beside it is exactly what the SDP's `traceprinter` printed for it. qbridge decodes the raw file itself, with no SDP, and gets the same per-thread states (`tests/test_kevbin.py` checks this):

```
qbridge trace --file examples/traces/msgpass.kev.gz
```

These five traces have thread, process and system events only, so a waiting thread's holder is a hint here. For traces recorded with every class on, where qbridge names the holder from kernel calls and messages, see [tests/fixtures/kev](../../tests/fixtures/kev/README.md).

Your own trace works the same way: record it with `tracelogger` on the instrumented kernel (`procnto-smp-instr`) and pass the `.kev` file. Record without `-F1` and `-F6` (keep kernel calls and communication) to have waits tied to their holders, and add `-k 128` if the kernel reports dropped events. `--decoder traceprinter` uses the SDP's traceprinter instead, if it is installed.
