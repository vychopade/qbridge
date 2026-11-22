# qbridge RT

**Debugging tools tell you what happened. qbridge RT lets an AI assistant find out why a real-time task missed its deadline, test a fix, and measure whether it worked, on a real QNX 8.0 target.**

Built for the QNX Global Hackathon (Developer Tooling & Host Tools). Open source under the MIT licence.

## The problem

In a real-time system a periodic task, such as a control loop, has to finish every cycle before its deadline. When it sometimes doesn't, the cause is rarely in the task itself. It might be a lower-priority thread holding a lock, a medium-priority thread taking the CPU, a late wake-up, or the previous cycle overrunning. Finding out which takes per-cycle timing, knowledge of who holds what and who waits for whom, and an understanding of priorities. Then the fix has to be tested on the target and *measured*, because a fix that looks right can still miss deadlines.

AI assistants can write QNX code, but they can't see the target, measure timing or run an experiment. qbridge RT gives them that, with a person approving every change.

## What it does

```
Detect  ->  Diagnose  ->  Propose  ->  (a person approves)  ->  Test  ->  Measure  ->  Verify
```

| Step | Tool (MCP / CLI) | What happens |
|---|---|---|
| Detect | `qnx_measure` / `qbridge measure` | Runs the workload for a fixed time. Every cycle's response is split into backlog, scheduling latency, lock wait, CPU time and preempted time, and each run of consecutive misses is attributed to what started it. The program's own numbers and qbridged's numbers are cross-checked. |
| Diagnose | `qnx_diagnose` | Takes thread-state snapshots from qbridged while the workload runs. Deterministic rules find a priority inversion on a mutex (the holder and the mutex protocol are read from the mutex in process memory) or CPU preemption (the deadline task is ready while a higher-priority thread has its CPU). Each finding lists its evidence, a confidence level and what it could not determine. With `trace`, a kernel trace from the same run checks every step of each finding; it is marked confirmed only if all of them are seen. |
| Propose | `qnx_experiment_propose` | One exact change, shown as a diff and not applied: an edit to one source file, or new workload arguments (a scheduling change such as a thread priority, with the same binary). |
| Approve | `qbridge experiment approve` | **Only a person, at an interactive terminal.** There is no MCP tool for it. The approval is tied to the diff's sha256. |
| Test | `qnx_experiment_run` | Applies the change, rebuilds with `qcc`, redeploys and reruns the identical workload. |
| Measure and verify | `qnx_experiment_run`, `qnx_report` | Compares before and after, with misses and worst-case response first. "Improved" means every run after the change beat every run before it; otherwise the result is "inconclusive". A self-contained HTML report is built only from recorded data, and `qnx_experiment_rollback` restores the baseline. |

### Result on the reference target

The demo workload [rtdemo](examples/rtdemo/README.md) has a 10 ms control task (priority 30), a CPU-heavy worker (20) and a logger (10) that shares a lock with the control task. The lock is created with `PTHREAD_PRIO_NONE`, which turns off QNX's default priority inheritance. That's the planted fault.

qbridge's diagnosis, live on the target:

> **Priority inversion on a mutex (high confidence).** 'A-control' (priority 30) waits for a mutex held by 'C-logger' (priority 10), while 'B-worker' (priority 20) keeps the CPU. Evidence: blocked in 9 of 25 snapshots for up to 32 ms; holder read from the mutex's owner word; middle-priority thread running in all 9; protocol flags say PTHREAD_PRIO_NONE; 76 of 76 runs of misses started with a long lock wait.

The fix it proposed (restore `PTHREAD_PRIO_INHERIT`), approved by a person at the prompt, then measured (3 runs of 10 s each way):

| | Before | After |
|---|---|---|
| Missed deadlines (of 999 cycles per run) | 310, 304, 302 | **0, 0, 0** |
| Worst response | 37.0–37.1 ms | **4.1–5.4 ms** |
| 99th percentile | 36.3–36.9 ms | 3.7–3.9 ms |
| Worst lock wait | 35.9–36.0 ms | 2.9 ms |

The verdict was "improved": every run after the change beat every run before it. It also stated its own limits: *based on 3 + 3 finite runs on this target; this does not prove a worst case, and virtual-machine timing adds noise.* The full report is [docs/example-report.html](docs/example-report.html) (download it and open it in a browser).

A kernel trace from the same kind of run confirmed each step of the finding: in one second, 'A-control' was blocked on the mutex for 351.9 ms. For 94% of that time 'C-logger' was ready but not running, still at priority 10, while 'B-worker' ran on its CPU. All 10 waits ended while 'C-logger' was running, which is when it released the lock.

**Second scenario: CPU overload with the wrong priorities.** `rtdemo -s overload -a 15` uses no lock; the control task runs at priority 15, below the CPU-heavy worker at 20. qbridge found **CPU preemption (high confidence)**: the control task was ready while 'B-worker' held its CPU, and every run of misses started with waiting for the CPU, none with a lock. The kernel trace confirmed it: the control task was ready for 334.4 ms of one second, and 'B-worker' ran on its CPU for 95% of that time. The proposed fix was a scheduling change with the same binary (`-a 30`), approved at the prompt and measured the same way:

| | Before (`-a 15`) | After (`-a 30`) |
|---|---|---|
| Missed deadlines (of 999 cycles per run) | 301, 309, 305 | **0, 1, 0** |
| Worst response | 36.1–46.2 ms | **5.0–10.6 ms** |
| 99th percentile | 35.5–36.1 ms | 3.0 ms |

Verdict "improved". The one miss after the change started with a late wake-up (9.2 ms), not with preemption; that day's timer check measured CPU 1's wake-ups up to 28.4 ms late. Report: [docs/example-report-overload.html](docs/example-report-overload.html).

## How it uses QNX

The target side is native QNX 8.0 C code. These are the places to look:

| Where | What | QNX technology |
|---|---|---|
| [target/qbridged/qbridged.c](target/qbridged/qbridged.c) `main`, `qb_read`, `qb_ocb_calloc` | A resource manager serving live system state as files under `/dev/qbridge` | `resmgr_attach`, `iofunc_*` layer with a custom OCB, `dispatch_*` |
| `walk_threads`, `emit_blocked`, `thread_name` | Every thread's state, priority, blocked-on details and how long it has been blocked | procfs `DCMD_PROC_INFO` / `DCMD_PROC_TIDSTATUS`, `ThreadCtlExt(_NTO_TCTL_NAME)` |
| `read_sync` | The holder and protocol of the mutex a thread is blocked on, read from the `sync_t` in that process's memory | `/proc/<pid>/as` with `pread64`; `_NTO_SYNC_*` flags |
| `setup_events`, `on_pulse`, `events_read`, `qb_unblock` | Process-death and core-dump events pushed by pulses; a read of `/dev/qbridge/events` blocks until something happens | `procmgr_event_notify_add`, `DCMD_DUMPER_NOTIFYEVENT`, `MsgRegisterEvent`, `pulse_attach`, held replies (`_RESMGR_NOREPLY`, `MsgReply`, `MsgError`) |
| [target/qbridged/qbridge_task.h](target/qbridged/qbridge_task.h) `qbt_register`, `qbt_report` | A real-time task registers once, then reports every cycle without ever blocking | `devctl` (a `MsgSend`) to register; `MsgSendPulse` per cycle |
| [target/qbridged/qb_deadlines.c](target/qbridged/qb_deadlines.c) `qbd_devctl`, `qbd_on_pulse` | Per-task deadline statistics in memory set aside at startup, served at `/dev/qbridge/deadlines`; pulses accepted only from the registering connection | resmgr devctl handler, pulse handler, kernel-supplied `scoid` |
| [examples/rtdemo/rtdemo.c](examples/rtdemo/rtdemo.c) `LOCK_PROTOCOL`, `task_a`, `setup_thread`, `scenario`, `a_prio` | The demo workload: periodic task, CPU pinning, FIFO priorities, the mutex protocol; two scenarios (`-s inversion`, `-s overload`) and the control task's priority (`-a`) | `pthread_mutexattr_setprotocol`, `clock_nanosleep(TIMER_ABSTIME)`, `ThreadCtl(_NTO_TCTL_RUNMASK)`, `SCHED_FIFO` |
| [target/tools/vmjitter.c](target/tools/vmjitter.c) `timer_mode` | Measures how late timers fire on each CPU (the environment's noise floor) | `clock_nanosleep`, run masks |
| [target/image/snippets](target/image/snippets) | Builds qbridged into the target image and starts it at boot | `mkqnximage` snippets |
| [docs/codelab/livedata](docs/codelab/livedata) `live_read`, `live_write`, `live_ocb_calloc` | The codelab's stand-alone example: a resource manager serving `/dev/livedata/uptime` (live, read-only) and `/dev/livedata/message` (writable) | `resmgr_attach`, custom attribute and OCB, `iofunc_read_verify` / `iofunc_write_verify`, `resmgr_msgget` |
| [src/qbridge/tools.py](src/qbridge/tools.py) `capture_trace`; [commands.py](src/qbridge/commands.py) `_rule_tracelogger` | Records a short kernel trace on the target in the same run as the diagnosis and decodes it on the host | instrumented kernel `procnto-smp-instr`, `tracelogger` (wide mode, class filters), SDP `traceprinter` |
| [src/qbridge/parsers/kev.py](src/qbridge/parsers/kev.py) `parse`; [src/qbridge/traceconfirm.py](src/qbridge/traceconfirm.py) `confirm_preemption`, `confirm_inversion` | Turns kernel events into per-thread state spans and checks each step of a finding against them | `THREAD` class events (`THRUNNING`, `THREADY`, `THMUTEX`, ...), `PROCTHREAD_NAME`, `CONTROL TIME` |

The host side (Python, no dependencies beyond the standard library) builds with the SDP's `qcc`, talks to the target over ssh, and analyses gdb output when crash analysis is used. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the pieces fit together.

## Quick check without a target

You can check the analysis on any computer with Python 3.10 or newer, without QNX or a target:

```
git clone https://github.com/vychopade/qbridge.git
cd qbridge
python -m venv .venv
.venv\Scripts\activate
pip install -e .
qbridge replay
```

`qbridge replay` runs qbridge's diagnosis rules, kernel-trace checks and experiment verdicts on data recorded on the reference target (thread snapshots, deadline reports, kernel traces and per-run reports, kept in `tests/fixtures/`). It then compares each result with what qbridge reported on the target at the time. It ends with:

```
7 of 7 results are the same as qbridge reported on the target.
```

It measures nothing new. It shows that the analysis is deterministic and reproduces the recorded results. On Linux or macOS, activate the venv with `source .venv/bin/activate`.

Then:
- `pip install -e ".[dev]"` and `pytest` run the full test suite, which also uses recorded target output.
- Open [docs/example-report.html](docs/example-report.html) (priority inversion, fixed in the source) and [docs/example-report-overload.html](docs/example-report-overload.html) (CPU preemption, fixed by a priority change) in a browser. These are the HTML reports from the recorded experiments, with the kernel trace timelines.
- The codelab, [docs/codelab](docs/codelab/README.md), walks through writing a small QNX resource manager; that part needs the QNX SDP and a target.

## Build and run

### What you need

| | Used and tested |
|---|---|
| Host | Windows 11 |
| QNX | QNX SDP 8.0 (QNX Everywhere licence): `qcc` with gcc 12.2.0 (`gcc_ntox86_64`), GNU make 4.4.1, `ntox86_64-gdb` 14.2 |
| Target | QNX 8.0 x86_64 image built with `mkqnximage`, running in VirtualBox 7.2 with 2 virtual CPUs |
| Python | 3.10 or newer (tested with 3.14); no third-party packages, and `pytest` for the tests |
| Other | Git, and Windows' built-in OpenSSH client |

The host code also supports `qnxsdp-env.sh` on Linux, but only a Windows host has been tested. The commands below are for a Windows Command Prompt.

### 1. Create the target

```
call %USERPROFILE%\qnx800\qnxsdp-env.bat
ssh-keygen -t ed25519 -f %USERPROFILE%\.ssh\qnx_target_ed25519 -N ""
mkdir %USERPROFILE%\qnx-target && cd %USERPROFILE%\qnx-target
mkqnximage --type=vbox --arch=x86_64 --ssh-ident=%USERPROFILE%/.ssh/qnx_target_ed25519.pub --run
```

On Windows, `mkqnximage` supports VirtualBox or VMware; QEMU needs a Linux host. VirtualBox needs a host-only network with DHCP, and the default one works.

### 2. Install qbridge on the host

```
git clone https://github.com/vychopade/qbridge.git
cd qbridge
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

The tests use output recorded on a real target, so they need no target.

qbridge finds the target's address with `mkqnximage --getip` in `%USERPROFILE%\qnx-target` and remembers it. Set `QBRIDGE_HOST` to skip that. Other settings are in [docs/TOOLS.md](docs/TOOLS.md#configuration).

### 3. Build the target programs and start qbridged at boot

```
qbridge build target\qbridged
qbridge image install
pushd %USERPROFILE%\qnx-target
mkqnximage --stop
mkqnximage --build --run
popd
qbridge ps --process qbridged
```

`qbridge build` runs `make` with the SDP environment loaded. Plain `make -C target\qbridged` works too, after `qnxsdp-env.bat`. `qbridge image install` copies qbridged and two `mkqnximage` snippets into the image folder; rebuilding the image puts qbridged at `/system/bin/qbridged` and starts it at boot. Rebuilding resets the target's `/data` partition.

Then build and copy the workload and the timing check to the target:

```
qbridge build examples\rtdemo
qbridge build target\tools
qbridge --allow-write deploy examples\rtdemo\build\rtdemo
qbridge --allow-write deploy target\tools\build\vmjitter
```

### 4. Check the target's timing first

```
qbridge --allow-write timing-check --cpu 1
qbridge --allow-write timing-check --cpu 0
```

This measures how late a high-priority 1 ms timer fires with nothing else running. **On the reference VirtualBox VM, CPU 0's timers were 15–19 ms late on average (worst about 55–63 ms), while CPU 1 usually stayed within a few ms (worst 1.9–2.3 ms; one noisier session on Nov 22 reached 28 ms).** That's why rtdemo runs on CPU 1 (`-c` to change it). The report quotes these checks.

### 5. Run the workflow

```
qbridge --allow-write measure rtdemo --seconds 10 --label baseline
qbridge --allow-write diagnose rtdemo --seconds 8 --trace

qbridge --allow-write experiment start rtdemo --project examples\rtdemo --repeats 3
qbridge experiment propose <id> --file rtdemo.c --find "#define LOCK_PROTOCOL PTHREAD_PRIO_NONE" --replace "#define LOCK_PROTOCOL PTHREAD_PRIO_INHERIT" --description "Restore priority inheritance on the shared lock"
qbridge experiment approve <id>
qbridge --allow-write experiment run <id>
qbridge report <id>
qbridge --allow-write experiment rollback <id>
```

`experiment approve` shows the diff and waits for you to type `yes`. Run it in a normal terminal window. `report` writes `%USERPROFILE%\.qbridge\reports\<id>.html`. `rollback` puts the original source and binary back, which keeps the demo's fault in place.

### 6. Let an AI assistant drive it

qbridge is also an MCP server on stdio. For Claude Code:

```
claude mcp add qbridge -- C:\path\to\qbridge\.venv\Scripts\qbridge.exe serve --allow-write
```

Leave out `--allow-write` to give the assistant read-only access. Any MCP client that can start a stdio server works the same way. The assistant can measure, diagnose, propose and run experiments, but it cannot approve its own change.

## Safety

- **Read-only by default.** Anything that changes the target needs `--allow-write`, and the assistant cannot switch that on.
- **A person approves every source change** at an interactive prompt, tied to the exact diff.
- **Command allowlist.** There is no free-form command tool. Every command sent to the target is checked against a list of programs and argument rules, and scripts are allowed only as fixed templates. Uploads and program runs are confined to `/data/qbridge`, and only programs qbridge deployed can be signalled.
- **Timeouts on everything; rollback** to the saved baseline binary, checked by sha256.
- **Audit log** of every action, including refusals and approvals, in a hash chain (`qbridge audit --verify`). It is tamper-evident, not tamper-proof.

[docs/TOOLS.md](docs/TOOLS.md#guard-rails) shows where each guard rail is enforced and which tests check it.

## What the measurements do and do not show

- They come from **finite runs on a QNX 8.0 virtual machine**. They show the effect of a change on this workload; they do not prove a worst-case response time or that a deadline can never be missed.
- Virtual-machine timing is noisier than real hardware, and absolute numbers vary between boots (the faulty build missed about 300 deadlines in one boot and 384 in another). Only runs from the same session are compared.
- rtdemo is an **educational, simulated** workload. Nothing here is a claim about any automotive, medical or other safety-certified system.
- The mutex owner's layout in memory was **observed** on QNX 8.0, not taken from documentation. qbridged checks it against the process ID on every read, and findings say so.

## Also included

The general QNX debugging tools from the project's first phase still work and are documented in [docs/TOOLS.md](docs/TOOLS.md):
- process and thread listing with deadlock and message-passing inversion detection;
- slog2 logs and process-death and core-dump events;
- crash analysis with gdb;
- three more demo programs with planted bugs ([examples/](examples/README.md)).

## Repository layout

| Path | Contents |
|---|---|
| `src/qbridge/` | Host side: MCP server (`server.py`), CLI (`cli.py`), tools, measurement and analysis, experiments, report, safety |
| `target/qbridged/` | The QNX resource manager and the task reporting header ([README](target/qbridged/README.md)) |
| `target/tools/` | `vmjitter`, the timing check |
| `target/image/` | `mkqnximage` snippets to build qbridged into the image |
| `examples/rtdemo/` | The real-time demo workload ([README](examples/rtdemo/README.md)) |
| `examples/` | Other demo programs |
| `tests/` | The tests; `tests/fixtures/` holds output recorded on the real target (also used by `qbridge replay`) |
| `docs/` | [ARCHITECTURE.md](docs/ARCHITECTURE.md), [TOOLS.md](docs/TOOLS.md), [DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md), [SUBMISSION.md](docs/SUBMISSION.md), the two example reports |
| `docs/codelab/` | The codelab "Write a QNX resource manager that serves live data as files" and its example |

## Licence

MIT, see [LICENSE](LICENSE).
