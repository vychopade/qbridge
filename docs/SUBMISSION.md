# Devpost submission text (draft)

A draft for each field of the submission form. Facts and numbers come from the repository and the recorded runs. Sections marked **[your words]** are about you and your experience: please write those yourself.

---

## Project name

qbridge RT

## Tagline

Debugging tools tell you what happened. qbridge RT lets an AI assistant find out why a real-time task missed its deadline, test a fix, and measure whether it worked.

## Category

Developer Tooling & Host Tools.

Bonus prizes (a project can win only one): **[your choice]**. The project fits Best Use of QNX-specific Technologies (message passing, resource managers, real-time services, kernel tracing). The codelab in `docs/codelab` was written for Best Codelab / Tutorial, which needs the codelab's own URL on the form (for example https://github.com/vychopade/qbridge/blob/main/docs/codelab/README.md, or a QNX Codelabs page if you export it with `claat`).

## Inspiration

**[your words]** For example: why missed deadlines are hard to track down, and what you wanted an assistant to be able to do on a real QNX target.

## What it does

qbridge RT connects an AI coding assistant (any MCP client) or a developer's terminal to a QNX 8.0 target, and runs a complete loop for real-time timing problems:

1. **Detect.** It runs the workload for a fixed time and measures every cycle, split into scheduling latency, lock wait, CPU time, time preempted and backlog from an overrunning previous cycle. Runs of consecutive misses are attributed to what started them. The numbers are cross-checked three ways: the task's own measurements, statistics recomputed on the host, and statistics gathered on the target by a QNX resource manager through message passing.
2. **Diagnose and confirm.** It snapshots every thread's state through the resource manager while the workload runs and applies deterministic rules. One finds a priority inversion on a mutex: it reads who holds the mutex and its protocol directly from process memory. The other finds CPU preemption: the deadline task is ready while a higher-priority thread holds its CPU. Each finding lists its evidence, a confidence level and what it could not determine. Then, optionally, it records a one-second kernel trace with `tracelogger` in the same run and checks every step of the finding against the thread state changes. A finding is marked "confirmed by kernel trace" only if every step is seen; otherwise the report lists the steps the trace did not show.
3. **Propose, approve, test.** The assistant proposes one exact change, shown as a diff: a source edit, or new workload arguments for a scheduling change such as a thread priority. Only a person can approve it, by typing "yes" at an interactive terminal; there is no tool for an assistant to approve itself, and the approval is tied to the diff's hash. qbridge then rebuilds with the QNX toolchain, redeploys and reruns the identical workload.
4. **Measure and verify.** It compares before and after, with misses and worst-case response first. It says "improved" only if every run after the change beat every run before it; otherwise "inconclusive". It writes a self-contained HTML report built only from recorded data, including what the measurements do not prove, and can roll back to the baseline binary, checked by sha256.

Results on the demo workload, a 10 ms control task, over three runs of 999 cycles each way on the target:

- **Priority inversion** (the lock created without priority inheritance). The diagnosis found it with high confidence, and the kernel trace confirmed each step: the control task blocked on the mutex for 351.9 ms of one second, the holder waited for the CPU at its own low priority for 94% of that time, and the middle-priority worker ran instead. The approved one-line fix took misses from 310/304/302 to 0/0/0 and the worst response from about 37 ms to 4.1–5.4 ms.
- **CPU overload with the wrong priorities** (the control task below a CPU-heavy worker). The diagnosis found CPU preemption, and the trace confirmed it: the control task was ready for 334.4 ms of one second, and the worker ran on its CPU for 95% of that time. Raising the control task's priority, with no code change, took misses from 301/309/305 to 0/1/0 and the worst response from 36.1–46.2 ms to 5.0–10.6 ms.

Anyone can check the analysis without a target: `qbridge replay` reruns the diagnosis rules, the trace checks and the experiment verdicts on the data recorded on the target, and reports that all 7 results are the same as on the target.

## How we built it

- **Target side: native QNX 8.0 C.**
  - `qbridged` is a resource manager (`resmgr_attach`, the `iofunc` layer with a custom OCB) serving `/dev/qbridge/*`.
  - Thread state comes from procfs (`DCMD_PROC_INFO`, `DCMD_PROC_TIDSTATUS`). The mutex holder and protocol come from the `sync_t` in the waiting process's memory (`/proc/<pid>/as`, `_NTO_SYNC_*` flags).
  - Process-death and core-dump events arrive as pulses (`procmgr_event_notify_add`, `DCMD_DUMPER_NOTIFYEVENT`, `MsgRegisterEvent`). Readers of `/dev/qbridge/events` stay blocked on a held reply until an event arrives.
  - Real-time tasks register once with a `devctl` and report every cycle with `MsgSendPulse` (`qbridge_task.h`), so they never block on the monitor. qbridged keeps their statistics in memory set aside at startup.
  - The demo workload uses `SCHED_FIFO` priorities, CPU run masks, `clock_nanosleep(TIMER_ABSTIME)` and `pthread_mutexattr_setprotocol`, and has two scenarios (`-s inversion`, `-s overload`).
  - Kernel traces are recorded with `tracelogger` on the instrumented kernel (`procnto-smp-instr`), in wide mode with the kernel-call, interrupt and communication classes off, and decoded on the host with the SDP's `traceprinter`. qbridge turns the `THREAD` events into per-thread state spans, handling the 32-bit time stamp that wraps about every second.
  - qbridged is built into the target image with `mkqnximage` snippets and starts at boot.
- **Host side: Python, standard library only.** An MCP server written directly on JSON-RPC over stdio, a CLI with the same tools, a build step with the SDP's `qcc` that returns structured compiler errors, statistics and miss attribution, the diagnosis rules and their trace checks, the experiment state machine, and an HTML report with inline SVG charts and a kernel-trace timeline.
- **Safety.**
  - Read-only by default.
  - Human approval for every source change.
  - A command allowlist at the single point where commands leave for the target, with shell scripts only as fixed templates.
  - Everything confined to one deploy folder, with timeouts.
  - Rollback, and a hash-chained audit log of every action and refusal.
- **Testing.** 243 automated tests run against output recorded on the real target, so they need no target. Every feature was also run against the real target before it was called done, and the README's commands were rerun from a fresh clone.
- **Codelab.** "Write a QNX resource manager that serves live data as files" (`docs/codelab`) teaches the technique qbridged is built on, with its own two-step example. Every command and output in it was run on the target.

## Challenges we ran into

- **The virtual machine's own timing.** The first version of the demo missed about a quarter of its deadlines even with the fix applied. We tested and ruled out two explanations, then built a timer-latency check (`vmjitter`). It showed that on this VirtualBox VM, timers on virtual CPU 0 fire 15–19 ms late on average and up to about 55–63 ms, even when idle, while CPU 1 usually stays within a few milliseconds (one session reached 28 ms, which the reports then quoted). The demo now runs on CPU 1, and every report quotes the timer lateness measured closest to its runs.
- **Who holds a mutex.** The kernel reports which mutex a thread waits on, but not its owner. We read the mutex from the process's memory. The protocol flags are documented, but the owner field's layout is not, so we worked it out on the target, check it against the process ID on every read, and say so in every finding.
- **Message-passing details found only on the target.** A task's `close()` overtook its own last low-priority pulses, losing the final cycle. The rcvid of a client that gives up arrives in the unblock pulse's value, not where we first looked. Both were found by comparing qbridged's numbers with the task's own, and fixed.
- **Reading the kernel trace correctly.** The trace's time stamps are the low 32 bits of a 3.9 GHz cycle counter, so they wrap about every 1.1 seconds; we track the wrap per CPU. The mutex event names the waiting thread but not the owner, so the holder still comes from the snapshot, and the report says so. A bug in our own fixture filter, which attached stray lines to the wrong event, was caught by checking every thread's totals against the unfiltered trace.
- **Being honest about what a measurement proves.** We made the verdict strict ("improved" only if every run after beat every run before), kept execution time, latency and lock wait separate, and wrote the limits into every report.

## Accomplishments that we're proud of

**[your words]** Possible points: the complete loop working end to end on a real target, for two different faults; the three-way cross-check agreeing exactly; the diagnosis reading mutex ownership from process memory and being confirmed step by step by a kernel trace; human approval that an assistant cannot get around.

## What we learned

**[your words]** Possible points: QNX message passing and resource managers in practice; why measurement noise has to be measured before results can be trusted.

## What's next for qbridge RT

- Run on real hardware (a Raspberry Pi 5) and compare with the virtual machine.
- Catch rare stalls: one 376 ms stall of the control task was seen once during a diagnosis run and never inside a trace window, so its cause is still unknown. Longer, repeated traces could catch it.
- More rules: deadlocks and message-passing inversions are already detected in the general tools; next is priority-ceiling misuse.

## Built with

QNX SDP 8.0, QNX Neutrino RTOS 8.0, C, `qcc` (gcc 12.2), `tracelogger`, `traceprinter`, `mkqnximage`, VirtualBox, Python, Model Context Protocol (MCP), SVG.

## Links

- Repository: https://github.com/vychopade/qbridge (MIT licence)
- Demo video: **[YouTube link]**
- Build and run instructions: README, "Build and run"
- QNX-specific code: README, "How it uses QNX", and [docs/ARCHITECTURE.md](ARCHITECTURE.md)
- Check without a target: README, "Quick check without a target" (`qbridge replay`)
- Codelab: [docs/codelab/README.md](codelab/README.md)
- Example reports: [docs/example-report.html](example-report.html) (priority inversion), [docs/example-report-overload.html](example-report-overload.html) (CPU overload)

## GitHub About (set these on the repository page, gear icon next to "About")

- **Description:** Find out why a real-time task on QNX 8.0 missed its deadline, test a fix and measure it: an MCP server and CLI with a native QNX resource manager, kernel-trace-confirmed diagnosis, and human-approved experiments.
- **Website:** leave empty, or the demo video link once it is up.
- **Topics:** `qnx`, `qnx-neutrino`, `rtos`, `real-time`, `embedded`, `mcp`, `model-context-protocol`, `developer-tools`, `resource-manager`, `priority-inversion`, `scheduling`, `kernel-tracing`, `performance-analysis`, `ai-tools`
- Tick "Releases" and "Packages" off if unused, so the sidebar stays short.

## Before submitting

- [ ] Make the repository **public** (it is private now), then check that the About sidebar shows "MIT license".
- [ ] Record and upload the video as public (under 3 minutes; see [DEMO_SCRIPT.md](DEMO_SCRIPT.md)).
- [ ] Fill the **[your words]** sections above.
- [ ] Choose the bonus prize; if Best Codelab, put the codelab's URL in its field.
- [ ] Open the README on GitHub once it is public and click through the "How it uses QNX" links.

## Limits stated in the submission

The measurements come from finite runs on a QNX 8.0 virtual machine. They show the effect of the fix on this workload; they do not prove a worst-case response time. rtdemo is an educational, simulated workload; nothing here is a claim about automotive, medical or other certified systems.
