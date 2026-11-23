# qbridge reference

Every tool, what it does, and the details behind each step of the workflow. For an overview and setup, see the [README](../README.md); for how the pieces fit together, see [ARCHITECTURE.md](ARCHITECTURE.md).

Paths in this page are relative to the repository root.

## Tools

| MCP tool | CLI | Changes the target? | What it does |
|---|---|---|---|
| `qnx_info` | `qbridge info` | no | Address, QNX version, CPUs, memory |
| `qnx_ps` | `qbridge ps` | no | Processes and threads, priorities, states, the blocked-on graph, and plain-English **deadlock** and **priority inversion** findings |
| `qnx_logs` | `qbridge logs` | no | slog2 entries filtered by process, pid, time, severity, text |
| `qnx_build` | `qbridge build` | no (host only) | `make` in a folder or `qcc` on one file; errors and warnings with file/line/column |
| `qnx_deploy` | `qbridge --allow-write deploy` | yes | Copy a file into `/data/qbridge` on the target |
| `qnx_run` | `qbridge --allow-write run` | yes | Run a program from `/data/qbridge` with a timeout; reports the signal if it dies. `background` starts it and returns at once (output to `<program>.log`) |
| `qnx_kill` | `qbridge --allow-write kill` | yes | Signal programs qbridge deployed (never system processes) and return their log; `ABRT` makes dumper write a core of a hung program |
| `qnx_gate` | `qbridge --allow-write gate` | yes (runs a program) | Measure a workload `repeats` times and pass or fail it: at most `max_misses` misses per run, an optional worst-response limit, and no run worse than saved baseline runs. The CLI exits with code 1 on failure, for CI against a lab target |
| `qnx_measure` | `qbridge --allow-write measure` | yes (runs a program) | Run a periodic workload for a fixed time; deadline statistics with each cycle split into backlog, scheduling latency, lock wait, CPU time and preempted time; runs of consecutive misses attributed to what started them; saved as a run |
| `qnx_diagnose` | `qbridge diagnose [--trace]` | no (yes if it starts the workload or traces) | Snapshot thread states while the workload runs and apply diagnosis rules: priority inversion on a mutex (holder and protocol read from the mutex in process memory), priority inversion through message passing (the task is SEND- or REPLY-blocked on a server process running below it while a middle-priority thread has the server's CPU) and CPU preemption (the deadline task is ready while a higher-priority thread has its CPU). Each finding has evidence, a confidence level, what it could not determine, and a suggested fix. With `--trace`, each finding is also checked step by step against a kernel trace from the same run |
| `qnx_trace` | `qbridge --allow-write trace` | yes (runs `tracelogger`) | Record a short kernel trace and report, per thread, time in each state and who ran on its CPU during its longest waits |
| `qnx_timing_check` | `qbridge --allow-write timing-check` | yes (runs `vmjitter`) | Measure the target's timer lateness on one CPU (a priority-50 thread waking every 1 ms) and save it as the environment's noise floor; reports quote the latest check |
| `qnx_experiment_start` / `_propose` / `_run` / `_rollback` / `_keep` / `_show` | `qbridge experiment start` / `propose` / `run` / `rollback` / `keep` / `show` | yes for start, run and rollback | A measured fix experiment: baseline runs, one proposed change, rerun and compare, then roll back or keep. A person approves the change with `qbridge experiment approve`, which has no MCP tool. See [Experiments](#experiments-test-a-fix-and-measure-it) |
| `qnx_report` | `qbridge report` | no (host only) | A self-contained HTML report for an experiment, built only from saved data. See [Report](#report) |
| `qnx_deadlines` | `qbridge deadlines` | no | Live deadline statistics that qbridged keeps for tasks registered with it by message passing (also tasks you start yourself) |
| `qnx_audit` | `qbridge audit [--verify]` | no | The audit log of qbridge actions (including refusals and approvals) and a check of its hash chain |
| `qnx_runs` / `qnx_run_report` | `qbridge runs` / `qbridge run-report` | no | List saved runs; show one (including the measured binary's sha256) |
| `qnx_events` | `qbridge events` | no | Wait for process deaths and core dumps, pushed by `qbridged` (needs qbridged running) |
| `qnx_crashes` | `qbridge crashes` | no | Core files written by QNX's `dumper` |
| `qnx_crash` | `qbridge crash` | no | Fetch a core and its binary, run gdb on the host: signal, fault address, backtrace with args and locals, source, and a plain-English summary |

### Crash analysis example

`examples/crash_null` is a small program with a planted NULL pointer bug. After building, deploying and running it:

```
qbridge crash
```

returns, among the frames and locals:

```
"summary": "/data/qbridge/sensor died with SIGSEGV in apply_calibration() at sensor.c:38.
            Fault address 0x8 is in the first page: a NULL pointer dereference (field at
            offset 8). NULL pointer(s) in that frame: cal."
```

## Deadline monitoring

```
qbridge --allow-write measure rtdemo --seconds 10 --label baseline
```

runs the workload, fetches its per-cycle samples and computes the statistics on the host. They are cross-checked against the program's own summary and, when the workload reports to qbridged, against qbridged's statistics gathered by QNX message passing. All three agreed on every run so far. Measured on the reference target:

> A-control: 299 of 999 cycles missed the 10 ms deadline (29.9%); worst response 37.4 ms, 99th percentile 36.5 ms, mean 10.10 ms. The misses form 99 run(s) of consecutive misses (longest 4); what started each run: waiting for a lock (98), late wake-up (scheduling latency) (1).

Each run is saved under `~/.qbridge/runs/<run_id>/` (`meta.json` with the binary's sha256, the program's `summary.json`, the raw `samples.csv`, and qbridge's `report.json`), so comparisons and reports are built only from captured data.

**Workload convention.** A program works with `measure` if it accepts `-d <seconds> -o <samples.csv>`, writes one CSV row per cycle with `release_us` and `response_us` (and optionally `backlog_us`, `latency_us`, `lock_wait_us`, `exec_us`), and prints a JSON summary line with `period_us` and `deadline_us`. [rtdemo](../examples/rtdemo/README.md) is the reference.

## Diagnosis

```
qbridge --allow-write diagnose rtdemo --seconds 8
```

starts the workload, takes `/dev/qbridge/threads` snapshots while it runs, reads that run's samples, and applies the rules. On the reference target, for the faulty rtdemo:

> **Priority inversion on a mutex (high confidence).** 'A-control' (priority 30) waits for a mutex held by 'C-logger' (priority 10), while 'B-worker' (priority 20) keeps the CPU.
> Evidence: A was blocked on the mutex in 9 of 25 snapshots, for up to 32 ms. The holder was C-logger at priority 10, read from the mutex's owner word in process memory (pid bits matched 9 of 9). B-worker had the holder's CPU in all 9. The protocol flags say PTHREAD_PRIO_NONE. 76 of 76 runs of consecutive misses in the same run started with a long lock wait.
> Could not determine: these are instants, not totals; the owner-word layout is observed, not documented; which source line uses the mutex; other delays; a worst case.

For the fixed build it reports no inversion ("this does not prove there is none; it was not observed"). It also reports priority inheritance seen working: the holder ran at priority 30 while its own priority is 10.

Confidence is high when at least three independent pieces of evidence agree: holder below the waiter, a middle-priority thread running, the PRIO_NONE flag, and lock-rooted deadline misses. With fewer it is medium or low, and the fix suggestion becomes more cautious. Diagnoses are saved under `~/.qbridge/diagnoses/` with the snapshots that support them.

## Experiments: test a fix and measure it

```
qbridge --allow-write experiment start rtdemo --project examples\rtdemo --repeats 3
qbridge experiment propose <id> --file rtdemo.c --find "<old line>" --replace "<new line>" --description "<why>"
qbridge experiment approve <id>          # a person, at an interactive prompt
qbridge --allow-write experiment run <id>
qbridge --allow-write experiment rollback <id>    # or: experiment keep <id>
```

The same steps are MCP tools (`qnx_experiment_start`, `_propose`, `_run`, `_rollback`, `_keep`, `_show`). The flow:

- **Start** builds the current source as the baseline, deploys it, measures the workload N times, and keeps the baseline binary and source files for rollback.
- **Propose** a single exact text replacement in one file of the project. It is shown as a diff and not applied.
- **Approve** has no MCP tool. `qbridge experiment approve` shows the diff and accepts only `yes` typed at an interactive terminal, so an assistant cannot approve its own change. The approval is tied to the diff's sha256; changing the proposal needs a new approval.
- **Run** applies the change, rebuilds (reverting the edit if the build fails), redeploys under the same name, and reruns the identical workload.
- **Compare** puts misses and worst-case response first. "improved" or "worse" means *every* run on one side beat every run on the other; overlapping results are reported as **inconclusive**.
- **Rollback** restores the source and redeploys the saved baseline binary, then checks the target's binary sha256 matches the baseline.

Live on the reference target (Nov 17, 2026), with the change approved by a person at the prompt:

| | Baseline (`PTHREAD_PRIO_NONE`) | Candidate (`PTHREAD_PRIO_INHERIT`) |
|---|---|---|
| Misses (3 runs, 999 cycles each) | 310, 304, 302 | 0, 0, 0 |
| Worst response | 37.0–37.1 ms | 4.1–5.4 ms |
| 99th percentile | 36.3–36.9 ms | 3.7–3.9 ms |
| Worst lock wait | 35.9–36.0 ms | 2.9 ms |

> Verdict: improved. Every run after the change beat every run before it. ... No deadline was missed after the change. Based on 3 + 3 finite runs on this target; this does not prove a worst case, and virtual-machine timing adds noise.

The experiment was then rolled back (the target's binary matched the baseline sha256 again), so the demo keeps its planted fault. The full record is in `tests/fixtures/experiment_live/`.

## Report

```
qbridge --allow-write timing-check --cpu 1      # saves the target's timer noise floor
qbridge report <experiment_id>                  # ~/.qbridge/reports/<id>.html
```

`qbridge report` (MCP: `qnx_report`) writes one self-contained HTML page, with no scripts and no external files. It is built only from saved data:

- the verdict and before/after headline numbers;
- per-run measurements;
- per-cycle response charts with the deadline line, and a response-time distribution;
- where the time went (each part of a cycle, and what started each run of misses);
- the diagnosis with evidence, confidence and what it could not determine;
- the change, who approved it and when, and a timeline;
- a recommendation;
- limitations: finite runs, VM timing, the measured timer lateness of the CPUs, the simulated-demo statement, and how many runs passed each cross-check;
- provenance: run ids and binary sha256 hashes.

[docs/example-report.html](example-report.html) is the report for the live experiment above (download it and open it in a browser).

## Kernel trace confirmation

Snapshots are instants. A kernel trace records every thread state change for a short window, so it can show whether a finding's story actually happens, and for how long. It needs the instrumented kernel (`procnto-smp-instr`, which the `mkqnximage` image uses) and `tracelogger` on the target; `traceprinter` from the SDP decodes the trace on the host.

```
qbridge --allow-write diagnose rtdemo --trace -- -s overload -a 15
```

runs the diagnosis as usual, then, while the same processes are still running, records one second with `tracelogger -w -n 0 -s 1 -F1 -F2 -F6` (wide events; kernel-call, interrupt and communication classes off, about 120 KB per second). The trace is fetched, decoded with `traceprinter`, and turned into per-thread state spans ([parsers/kev.py](../src/qbridge/parsers/kev.py)). Each finding is then broken into steps ([traceconfirm.py](../src/qbridge/traceconfirm.py)):

| Finding | Steps that must all be seen in the trace |
|---|---|
| CPU preemption | the task is READY (not blocked on a lock); the named higher-priority thread is RUNNING on the task's CPU for at least half of that READY time; it runs at a priority above the task's |
| Priority inversion | the waiter is MUTEX-blocked; the holder is READY below the waiter's priority for at least half of the wait (no inheritance); threads between the two priorities run on the holder's CPU for at least half of the holder's READY time; every completed wait ends while the holder is RUNNING (it released the mutex) |

Only if every step is seen is the finding marked **confirmed by kernel trace**; otherwise its confidence stays as it was and the report lists the steps the trace did not show. The trace covers about a second, so a pattern outside that window is "not shown", not disproved. QNX's `THMUTEX` event names the waiting thread but not the mutex or its owner, so the holder's identity still comes from the snapshot's mutex owner word.

Live results on Nov 22 (2-CPU VirtualBox VM, rtdemo on CPU 1, 1 s traces):

- **Overload (`-s overload -a 15`).** Confirmed. A-control was READY 334.4 ms in 71 spans with no MUTEX time; B-worker (priority 20) was RUNNING on that CPU for 319.2 ms of it (95%).
- **Inversion (`-s inversion`).** Confirmed. A-control was MUTEX-blocked 351.9 ms in 10 spans; C-logger was READY at priority 10 for 330.7 ms of that (94%), never raised to 30; B-worker (priority 20) ran for 330.5 ms of C-logger's READY time; 10 of 10 waits ended while C-logger was running.
- **Fixed overload (`-a 30`).** No finding, so nothing to confirm; the tests also point the fault's finding at this trace and check it is *not* confirmed.

The report draws the window around the longest wait: one lane per thread, coloured running / ready / blocked on a mutex. Raw `.kev` and decoded files stay in `~/.qbridge/traces/<id>/` (git ignores `*.kev`); the tests use small filtered, gzipped copies in `tests/fixtures/traces/`.

`qbridge --allow-write trace rtdemo -- -s overload -a 15` gives the same per-thread view without the diagnosis: time per state, and who ran instead during each thread's longest waits.

## Guard rails

- **Read-only by default.** `deploy` and `run` are refused unless qbridge was started with `--allow-write` (or `QBRIDGE_ALLOW_WRITE=1`). The assistant cannot switch this on itself.
- **One deploy folder.** Files can only be copied to, and programs only run from, `/data/qbridge` on the target. Paths like `../../proc/boot/ksh` are rejected.
- **A person approves every source change.** Experiments apply a change only after `qbridge experiment approve`, which needs `yes` typed at an interactive terminal and has no MCP equivalent. Changes are limited to files inside the experiment's project folder, and rollback restores both source and binary.
- **Only its own processes.** `kill` checks each process's program path and refuses anything outside the deploy folder, so the assistant cannot signal system services.
- **Time limits.** Programs run under QNX's `timeout`, background ones included, so a hung program cannot hang the assistant or outlive the session.
- **Command allowlist.** There is no free-form command tool, and every command qbridge sends to the target passes one check first ([commands.py](../src/qbridge/commands.py), called by `Target.run`). The program must be on the list and its arguments must match that program's rule; for example, `cat` only reads `/dev/qbridge/*` or the deploy folder, `kill` only sends allowed signals to numeric pids, and `slog2info` can't clear the log. Shell scripts are allowed only as named templates with validated, quoted parameters. Uploads go only into the deploy folder. Anything else is refused before ssh starts.
- **Audit log.** Every tool call, from the CLI or MCP, is appended to `~/.qbridge/audit.jsonl`: the tool and arguments, whether it can change the target, its outcome (`ok`, `refused` by a guard rail, or `error`), the target, and key results such as run ids, binary sha256, signals and approvals. Each entry carries the hash of the previous one, so `qbridge audit --verify` (MCP: `qnx_audit`) detects an edited or deleted entry. This is tamper-evident, not tamper-proof: someone who can rewrite the whole file can rebuild the chain.

### How each safety requirement is enforced

| Requirement | Where | Checked by |
|---|---|---|
| Read-only by default | `Policy.require_write` in every tool that changes the target | `tests/test_policy.py`, `test_audit.py` (refusals recorded) |
| Approval for source or scheduling changes | `qbridge experiment approve`: CLI only, needs an interactive terminal, tied to the diff's sha256. Priorities and protocols live in the source, so they go through the same approval | `tests/test_experiments.py` (no MCP approve tool, non-interactive refused, new diff needs new approval) |
| Command allowlist | `commands.check` at `Target.run`; upload check in `Target.put` | `tests/test_commands.py` (22 refused, 16 allowed forms) |
| Timeouts | `timeout` wrapper on every program run; ssh and scp time limits | `tests/test_server.py`, `test_demos.py` |
| Audit log | `audit.record` in `server.Server.call_tool` and `cli.main` | `tests/test_audit.py` (chain, edits, deletions, both interfaces) |
| Rollback to the baseline build | `experiments.rollback`: restores sources, redeploys the saved baseline binary, checks the target's sha256 | `tests/test_experiments.py`; live on Nov 17, sha256 matched |

Live check on Nov 21: the target refused writes in read-only mode, refused killing `qbridged` and running outside the deploy folder even with write access, and refused `slog2info -c`, `cat /etc/shadow` and a raw `sh -c` at the allowlist. All 14 actions were in the audit log, and the chain verified.

## Demo apps

[examples/](../examples/README.md) has three programs with planted bugs (a NULL dereference, a priority inversion and a message-passing deadlock) and what qbridge reports for each.

## Use it from an MCP client

qbridge's MCP server has no dependencies beyond Python. For Claude Code:

```
claude mcp add qbridge -- C:\path\to\qbridge\.venv\Scripts\qbridge.exe serve
```

Add `--allow-write` after `serve` to let the assistant deploy and run programs. Any MCP client that can start a stdio server works the same way: the command is `qbridge serve [--allow-write]`.

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
- `dumper` runs as `dumper -U 30:30 -d /data/var/dumper -M 1000 -D cores`, so core files for root's processes land in `/data/var/dumper/<program>.core`, overwritten on each crash.
- A process killed by a signal shows exit code 128+N in the target shell (SIGSEGV is 139).
- Kernel tracing: the target has `/system/bin/tracelogger` and runs the instrumented kernel `procnto-smp-instr`; `traceprinter` is not on the target, so traces are decoded on the host with the SDP's `traceprinter`. Its time stamps are the low 32 bits of the cycle counter (3,901,841,142 cycles per second here), which wrap about every 1.1 s; `CONTROL TIME` events carry the high bits.
- gdb is the SDP's `ntox86_64-gdb` (14.2). Shared libraries are resolved from `$QNX_TARGET/x86_64/lib` and `usr/lib`.
