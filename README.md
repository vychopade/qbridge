# qbridge RT

Debugging tools tell you what happened. qbridge RT lets an AI assistant find out why a real-time task missed its deadline, test a fix, and measure whether it worked.

> Direction: qbridge started as general remote debugging for QNX (the tools below still work). It is now focused on real-time deadline analysis: detect, diagnose, propose, approve, test, measure, verify. The demo workload is [examples/rtdemo](examples/rtdemo/README.md). Deadline monitoring, the experiment loop and the report are in progress.
>
> **Timing limitation:** all measurements so far come from a QNX 8.0 VirtualBox VM, where timers on virtual CPU 0 fire tens of milliseconds late (see [rtdemo's notes](examples/rtdemo/README.md#timing-on-a-virtual-machine-read-this) and `target/tools/vmjitter`). Finite runs show effects; they do not prove worst-case timing.

AI assistants can write QNX code but can't see the target. qbridge gives them eyes and hands on QNX, with guard rails.

qbridge lets any MCP client (or you, from the command line) build, deploy, inspect and debug software on a QNX 8.0 target.

> Status: early work for the QNX Global Hackathon. Working today: MCP server and CLI with `info`, `ps`, `logs`, `build`, `deploy`, `run`, `events` and crash analysis, plus the `qbridged` resource manager on the target with pulse-driven events. Also: three demo apps with planted bugs, and deadlock / priority-inversion detection in `ps`.

## Two halves

- **Host side** (`src/qbridge`, Python): the MCP server and CLI. Talks to the target over ssh.
- **Target side** ([target/qbridged](target/qbridged/README.md), C): a QNX resource manager that serves processes, threads, the blocked-on graph and crashes as JSON under `/dev/qbridge`, read straight from procfs. When it is running, `qnx_ps` uses it (and says `"source": "qbridged"`); otherwise it falls back to parsing `pidin`.

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
| `qnx_measure` | `qbridge --allow-write measure` | yes (runs a program) | Run a periodic workload for a fixed time; deadline statistics with each cycle split into backlog, scheduling latency, lock wait, CPU time and preempted time; runs of consecutive misses attributed to what started them; saved as a run |
| `qnx_diagnose` | `qbridge diagnose` | no (yes if it starts the workload) | Snapshot thread states while the workload runs and apply diagnosis rules; currently priority inversion on a mutex, with the holder and protocol read from the mutex in process memory. Each finding has evidence, a confidence level, what it could not determine, and a suggested fix |
| `qnx_deadlines` | `qbridge deadlines` | no | Live deadline statistics that qbridged keeps for tasks registered with it by message passing (also tasks you start yourself) |
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

**Workload convention.** A program works with `measure` if it accepts `-d <seconds> -o <samples.csv>`, writes one CSV row per cycle with `release_us` and `response_us` (and optionally `backlog_us`, `latency_us`, `lock_wait_us`, `exec_us`), and prints a JSON summary line with `period_us` and `deadline_us`. [rtdemo](examples/rtdemo/README.md) is the reference.

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

## Guard rails

- **Read-only by default.** `deploy` and `run` are refused unless qbridge was started with `--allow-write` (or `QBRIDGE_ALLOW_WRITE=1`). The assistant cannot switch this on itself.
- **One deploy folder.** Files can only be copied to, and programs only run from, `/data/qbridge` on the target. Paths like `../../proc/boot/ksh` are rejected.
- **A person approves every source change.** Experiments apply a change only after `qbridge experiment approve`, which needs `yes` typed at an interactive terminal and has no MCP equivalent. Changes are limited to files inside the experiment's project folder, and rollback restores both source and binary.
- **Only its own processes.** `kill` checks each process's program path and refuses anything outside the deploy folder, so the assistant cannot signal system services.
- **Time limits.** Programs run under QNX's `timeout`, background ones included, so a hung program cannot hang the assistant or outlive the session.

## Demo apps

[examples/](examples/README.md) has three programs with planted bugs (a NULL dereference, a priority inversion and a message-passing deadlock) and what qbridge reports for each.

## Use it from an MCP client

qbridge's MCP server has no dependencies beyond Python. For Claude Code:

```
claude mcp add qbridge -- C:\path\to\qbridge\.venv\Scripts\qbridge.exe serve
```

Add `--allow-write` after `serve` to let the assistant deploy and run programs. Any MCP client that can start a stdio server works the same way: the command is `qbridge serve [--allow-write]`.

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
   qbridge build examples\hello
   qbridge --allow-write deploy examples\hello\build\hello
   qbridge --allow-write run --timeout 10 hello first "second arg"
   ```

   Options for `run` go before the program name; everything after it is passed to the program.

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
- `dumper` runs as `dumper -U 30:30 -d /data/var/dumper -M 1000 -D cores`, so core files for root's processes land in `/data/var/dumper/<program>.core`, overwritten on each crash.
- A process killed by a signal shows exit code 128+N in the target shell (SIGSEGV is 139).
- gdb is the SDP's `ntox86_64-gdb` (14.2). Shared libraries are resolved from `$QNX_TARGET/x86_64/lib` and `usr/lib`.

## Tests

Tests run against output recorded from a real target (`tests/fixtures/`), so no target is needed:

```
pytest
```

## Licence

MIT, see [LICENSE](LICENSE).
