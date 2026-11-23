# Devpost submission text (draft)

A draft for each field of the submission form. Facts and numbers come from the repository and the recorded runs. Sections marked **[your words]** are about you and your experience: please write those yourself, in your own voice. Judges score "genuine understanding of the problem space", so these matter.

---

## Project name

qbridge RT

## Tagline

Debugging tools tell you what happened. qbridge RT lets an AI assistant find out why a real-time task on QNX 8.0 missed its deadline, test a fix, and measure whether it worked.

## Category

Developer Tooling & Host Tools.

**Bonus prizes:** you don't have to choose. Every eligible submission is considered for **Best Use of QNX-specific Technologies**. For **Best Codelab / Tutorial**, put the codelab's URL in its field: https://vychopade.github.io/qbridge/codelab/ (or the GitHub page, https://github.com/vychopade/qbridge/blob/main/docs/codelab/README.md). A project can win at most one bonus prize.

## When it was built

qbridge RT was created during the hackathon's Submission Period: the first commit is November 16, 2026, and all of it was written after November 2, 2026.

## Inspiration

**[your words]** For example: why missed deadlines are hard to track down, and what you wanted an assistant to be able to do on a real QNX target.

## What it does

qbridge RT connects an AI coding assistant (any MCP client) or a developer's terminal to a QNX 8.0 target, and runs a complete loop for real-time timing problems:

1. **Detect.** It runs the workload for a fixed time and measures every cycle, split into scheduling latency, lock wait, waiting for a server's reply, CPU time, time preempted and backlog from an overrunning previous cycle. Runs of consecutive misses are attributed to what started them. The numbers are cross-checked three ways: the task's own measurements, statistics recomputed on the host, and statistics gathered on the target by a QNX resource manager through message passing.
2. **Diagnose and confirm.** It snapshots every thread's state through the resource manager while the workload runs and applies deterministic rules:
   - **Priority inversion on a mutex.** It reads who holds the mutex, and its protocol, directly from process memory.
   - **Priority inversion through message passing,** a QNX-specific fault. The task waits for a reply from a server process whose thread runs below it while a middle-priority thread has the server's CPU. On QNX a server normally takes its client's priority, so this means inheritance is off, for example because the channel was created with `_NTO_CHF_FIXED_PRIORITY`.
   - **CPU preemption.** The deadline task is ready while a higher-priority thread holds its CPU.

   Each finding lists its evidence, a confidence level and what it could not determine. Then it records a one-second kernel trace with `tracelogger` in the same run and checks every step of the finding against the thread state changes. A finding is marked "confirmed by kernel trace" only if every step is seen.
3. **Propose, approve, test.** The assistant proposes one exact change, shown as a diff: a source edit, or new workload arguments for a scheduling change. Only a person can approve it, by typing "yes" at an interactive terminal; there is no tool for an assistant to approve itself, and the approval is tied to the diff's hash. qbridge then rebuilds with the QNX toolchain, redeploys and reruns the identical workload.
4. **Measure and verify.** It compares before and after, with misses and worst-case response first. It says "improved" only if every run after the change beat every run before it; otherwise "inconclusive". It writes a self-contained HTML report built only from recorded data, including what the measurements do not prove, and can roll back to the baseline binary, checked by sha256.
5. **Keep it fixed.** `qbridge gate` runs the same measurement as a pass/fail check with an exit code, for a CI job with a lab target.

Results on the demo workload, a 10 ms control task, over three runs of 999 cycles each way on the target:

| Fault | Diagnosis (confirmed by kernel trace) | Approved fix | Misses before → after | Worst response before → after |
|---|---|---|---|---|
| Mutex created with `PTHREAD_PRIO_NONE` | Priority inversion on a mutex | `PTHREAD_PRIO_INHERIT` | 310, 304, 302 → 0, 0, 0 | 37.0-37.1 → 4.1-5.4 ms |
| Server channel with `_NTO_CHF_FIXED_PRIORITY` | Priority inversion through message passing | drop the flag | 825, 819, 812 → 9, 4, 24 | 54.2-96.0 → 19.0-28.0 ms |
| Control task below a CPU-heavy thread | CPU preemption | priority 15 → 30, same binary | 301, 309, 305 → 0, 1, 0 | 36.1-46.2 → 5.0-10.6 ms |

The kernel trace showed each mechanism directly:
- **Mutex.** The control task was blocked on the mutex for 351.9 ms of one second. The holder waited for the CPU at its own low priority for 94% of that time, and the middle-priority worker ran instead.
- **Message passing.** Whenever the server was not doing the work during the control task's waits, it sat ready at priority 10 (100% of that time) while the worker ran.
- **Overload.** The control task was ready for 334.4 ms of one second, and the worker ran on its CPU for 95% of that time.

After the message-passing fix a few misses remained. In those cycles the server's 4 ms of work and the task's own 1 ms took several times longer, with nothing at their priority competing for the CPU. That points to the virtual machine slowing the guest; the report says so and that it is not proven.

Anyone can check the analysis without a target: `qbridge replay` reruns the diagnosis rules, the trace checks and the experiment verdicts on the data recorded on the target, and reports that all 11 results are the same as on the target.

## How we built it

- **Target side: native QNX 8.0 C.**
  - `qbridged` is a resource manager (`resmgr_attach`, the `iofunc` layer with a custom OCB) serving `/dev/qbridge/*`.
  - Thread state comes from procfs (`DCMD_PROC_INFO`, `DCMD_PROC_TIDSTATUS`). The mutex holder and protocol come from the `sync_t` in the waiting process's memory (`/proc/<pid>/as`, `_NTO_SYNC_*` flags). For SEND- and REPLY-blocked threads, the server process comes from the kernel's blocked-on information.
  - Process-death and core-dump events arrive as pulses (`procmgr_event_notify_add`, `DCMD_DUMPER_NOTIFYEVENT`, `MsgRegisterEvent`). Readers of `/dev/qbridge/events` stay blocked on a held reply until an event arrives.
  - Real-time tasks register once with a `devctl` and report every cycle with `MsgSendPulse` (`qbridge_task.h`), so they never block on the monitor. qbridged keeps their statistics in memory set aside at startup.
  - The demo workload uses `SCHED_FIFO` priorities, CPU run masks, `clock_nanosleep(TIMER_ABSTIME)` and `pthread_mutexattr_setprotocol`. Its message-passing scenario forks a server process that answers with `MsgReceive`/`MsgReply` on a channel made with `ChannelCreate(_NTO_CHF_FIXED_PRIORITY | _NTO_CHF_DISCONNECT)`, reached through `ConnectAttach` and `MsgSend`.
  - Kernel traces are recorded with `tracelogger` on the instrumented kernel (`procnto-smp-instr`) and decoded on the host with the SDP's `traceprinter`. qbridge turns the `THREAD` events (`THRUNNING`, `THREADY`, `THMUTEX`, `THSEND`, `THREPLY`, ...) into per-thread state spans, handling the 32-bit time stamp that wraps about every second.
  - qbridged is built into the target image with `mkqnximage` snippets and starts at boot.
- **Host side: Python, standard library only.**
  - An MCP server written directly on JSON-RPC over stdio, and a CLI with the same tools.
  - A build step with the SDP's `qcc` that returns structured compiler errors.
  - Statistics and miss attribution.
  - The diagnosis rules and their trace checks.
  - The experiment state machine.
  - The gate.
  - An HTML report with inline SVG charts and a kernel-trace timeline.
- **Safety.**
  - Read-only by default.
  - Human approval for every change.
  - A command allowlist at the single point where commands leave for the target, with shell scripts only as fixed templates.
  - Everything confined to one deploy folder, with timeouts.
  - Rollback, and a hash-chained audit log of every action and refusal.
- **Testing.** 253 automated tests run against output recorded on the real target, so they need no target. Every feature was also run against the real target before it was called done, and the README's commands were rerun from a fresh clone.
- **Codelab.** "Write a QNX resource manager that serves live data as files" (`docs/codelab`) teaches the technique qbridged is built on, with its own two-step example. Every command and output in it was run on the target.

## How it meets the judging criteria

For human and automated judging alike, this section points to the evidence for each criterion.

- **Technological Implementation.**
  - Native QNX 8.0 C on the target: `target/qbridged/qbridged.c` (`resmgr_attach`, `iofunc`, `read_sync` reading `sync_t` from `/proc/<pid>/as`, held replies), `qb_deadlines.c` (devctl and pulse handlers) and `qbridge_task.h` (`MsgSendPulse`).
  - `examples/rtdemo/rtdemo.c` (`run_server`: `ChannelCreate`, `MsgReceive`, `MsgReply`).
  - Kernel traces from `tracelogger`, decoded and checked step by step (`src/qbridge/parsers/kev.py`, `src/qbridge/traceconfirm.py`).
  - 253 tests, and `qbridge replay` reproduces all 11 recorded results.
  - The README's "How it uses QNX" table names every file and function, and a test checks that they exist.
- **Design.**
  - One loop from detection to a verified, reversible fix, with the same tools in an AI assistant (MCP) and a terminal (CLI).
  - A person approves every change.
  - Self-contained HTML reports, including their limits.
  - A CI gate.
  - A project site for judges, and a no-target quick check.
- **Potential Impact.**
  - Control loops in robots, industrial machines, medical devices and vehicles must meet deadlines, and rare misses caused elsewhere in the system are slow to find by hand.
  - QNX fits because it provides fixed-priority preemptive scheduling, priority inheritance on mutexes and through message passing, and a detailed kernel trace. qbridge RT turns those into a repeatable diagnose-fix-measure loop.
- **Innovation & AI Creativity.**
  - The assistant doesn't just read logs: it runs measured experiments on a real QNX target, and it can't approve its own changes.
  - Each diagnosis is backed by a trace that checks every step.
  - The message-passing rule knows a QNX-specific mechanism (server priority inheritance and `_NTO_CHF_FIXED_PRIORITY`) that generic tools do not look for.
  - Found on the target and written into the tool: with message-passing inheritance, procfs shows the server's own priority changed, unlike a mutex boost.

## Challenges we ran into

- **The virtual machine's own timing.**
  - The first version of the demo missed about a quarter of its deadlines even with the fix applied.
  - We tested and ruled out two explanations, then built a timer-latency check (`vmjitter`). On this VirtualBox VM, timers on virtual CPU 0 fire 15-19 ms late on average, and up to about 55-63 ms late, even when idle. CPU 1 usually stays within a few milliseconds, but varies between sessions (worst 2-28 ms).
  - The demo now runs on CPU 1, and every report quotes the timer lateness measured closest to its runs.
- **Who holds a mutex.** The kernel reports which mutex a thread waits on, but not its owner. We read the mutex from the process's memory. The protocol flags are documented, but the owner field's layout is not, so we worked it out on the target, check it against the process ID on every read, and say so in every finding.
- **What inheritance looks like through message passing.** We expected a server working for a high-priority client to show a raised priority over its own "real" priority, as a mutex holder does. On the target, procfs showed both at the client's level: message-passing inheritance changes the server's priority itself. So qbridge reports a server at its client's priority as consistent with inheritance, not as proof of it.
- **Message-passing details found only on the target.** A task's `close()` overtook its own last low-priority pulses, losing the final cycle. The rcvid of a client that gives up arrives in the unblock pulse's value, not where we first looked. Both were found by comparing qbridged's numbers with the task's own, and fixed.
- **Reading the kernel trace correctly.**
  - The trace's time stamps are the low 32 bits of a 3.9 GHz cycle counter, so they wrap about every 1.1 seconds; we track the wrap per CPU.
  - The mutex and message events name only the waiting thread, not the owner or the server. Those still come from the snapshot, and the report says so.
  - A bug in our own fixture filter attached stray lines to the wrong event. It was caught by checking every thread's totals against the unfiltered trace.
- **Being honest about what a measurement proves.** We made the verdict strict ("improved" only if every run after beat every run before), kept execution time, latency, lock wait and server wait separate, and wrote the limits into every report.

## Accomplishments that we're proud of

**[your words]** Possible points:
- The complete loop working end to end on a real target, for three different faults.
- A QNX-specific diagnosis confirmed step by step by a kernel trace.
- The three-way cross-check agreeing exactly.
- Human approval that an assistant can't get around.

## What we learned

**[your words]** Possible points: QNX message passing, priority inheritance and resource managers in practice; why measurement noise has to be measured before results can be trusted.

## What's next for qbridge RT

- Run on real hardware (a Raspberry Pi 5) and compare with the virtual machine.
- Catch rare stalls: one 376 ms stall of the control task was seen once during a diagnosis run and never inside a trace window, so its cause is still unknown. Longer, repeated traces could catch it.
- More rules: deadlocks are already detected in the general tools; next are priority-ceiling misuse and servers with several worker threads.

## Built with

QNX SDP 8.0, QNX Neutrino RTOS 8.0, C, `qcc` (gcc 12.2), `tracelogger`, `traceprinter`, `mkqnximage`, VirtualBox, Python, Model Context Protocol (MCP), SVG.

## Links

- Repository: https://github.com/vychopade/qbridge (MIT licence)
- Project site with the reports: https://vychopade.github.io/qbridge/
- Demo video: **[YouTube link]**
- Build and run instructions: README, "Build and run"
- QNX-specific code: README, "How it uses QNX", and [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)
- Check without a target: README, "Quick check without a target" (`qbridge replay`)
- Codelab: https://vychopade.github.io/qbridge/codelab/ ([docs/codelab/README.md](../docs/codelab/README.md))
- Example reports: [mutex inversion](../docs/example-report.html), [message-passing inversion](../docs/example-report-msgpass.html), [CPU overload](../docs/example-report-overload.html)

## GitHub About (set these on the repository page, gear icon next to "About")

- **Description:** Find out why a real-time task on QNX 8.0 missed its deadline, test a fix and measure it: an MCP server and CLI with a native QNX resource manager, kernel-trace-confirmed diagnosis, and human-approved experiments.
- **Website:** https://vychopade.github.io/qbridge/
- **Topics:** `qnx`, `qnx-neutrino`, `rtos`, `real-time`, `embedded`, `mcp`, `model-context-protocol`, `developer-tools`, `resource-manager`, `priority-inversion`, `message-passing`, `scheduling`, `kernel-tracing`, `performance-analysis`, `ai-tools`
- Untick "Releases" and "Packages" if unused, so the sidebar stays short.

## Devpost image gallery

Upload the images in `hackathon/gallery/` (no third-party logos in any of them). Use `01-site.png` as the cover image.

## Before submitting

- [ ] Turn on GitHub Pages (Settings → Pages → Deploy from a branch → `main`, folder `/docs`), then open https://vychopade.github.io/qbridge/ and click every link.
- [ ] Set the About fields above, and check that the sidebar shows "MIT license".
- [ ] Record and upload the video as public (under 3 minutes; see [DEMO_SCRIPT.md](DEMO_SCRIPT.md)). Put its link here, in the README's judges' path and in `docs/index.html`.
- [ ] Fill the **[your words]** sections.
- [ ] Put the codelab URL in the Best Codelab field.
- [ ] Decide the licence question in `COMPETITORS.md` (publishing measurements) before submitting.
- [ ] Tag the submitted state: `git tag hackathon-submission` and `git push origin hackathon-submission`.
- [ ] Submit by **December 13** (the deadline is December 15, 2026, 2:00 pm ET).

## Limits stated in the submission

The measurements come from finite runs on a QNX 8.0 virtual machine. They show the effect of each fix on this workload; they do not prove a worst-case response time. rtdemo is an educational, simulated workload; nothing here is a claim about automotive, medical or other certified systems.
