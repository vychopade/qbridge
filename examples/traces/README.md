# Recorded kernel traces

Four one-second kernel traces from a QNX 8.0 virtual machine (2 CPUs), recorded with `tracelogger` while [rtdemo](../rtdemo/README.md) ran, and decoded with the SDP's `traceprinter`. Each is the full trace: every process on the target, not just rtdemo. They are gzipped text; qbridge reads them as they are.

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
| `overload.txt.gz` | `rtdemo -s overload -a 15` | CPU preemption: `A-control` (15) ready but not running while `B-worker` (20) has its CPU |

These are hints from thread states and priorities. A trace does not record what a blocked thread waits for, so a trace on its own cannot prove which thread held the mutex or answered the message. On a live target, `qbridge diagnose --trace` reads that from the target and checks each finding against a trace from the same run.

Your own trace works the same way: record it with `tracelogger` on the instrumented kernel (`procnto-smp-instr`), then either decode it with `traceprinter -f trace.kev -o trace.txt` and pass the text, or pass the `.kev` file directly if the QNX SDP is installed on this computer.
