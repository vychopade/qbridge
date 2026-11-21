# Devpost submission text (draft)

A draft for each field of the submission form. Facts and numbers come from the repository and the recorded runs. Sections marked **[your words]** are about you and your experience: please write those yourself.

---

## Project name

qbridge RT

## Tagline

Debugging tools tell you what happened. qbridge RT lets an AI assistant find out why a real-time task missed its deadline, test a fix, and measure whether it worked.

## Category

Developer Tooling & Host Tools. Also entered for Best Use of QNX-specific Technologies (message passing, resource managers, real-time services).

## Inspiration

**[your words]** For example: why missed deadlines are hard to track down, and what you wanted an assistant to be able to do on a real QNX target.

## What it does

qbridge RT connects an AI coding assistant (any MCP client) or a developer's terminal to a QNX 8.0 target, and runs a complete loop for real-time timing problems:

1. **Detect.** It runs the workload for a fixed time and measures every cycle, split into scheduling latency, lock wait, CPU time, time preempted and backlog from an overrunning previous cycle. Runs of consecutive misses are attributed to what started them. The numbers are cross-checked three ways: the task's own measurements, statistics recomputed on the host, and statistics gathered on the target by a QNX resource manager through message passing.
2. **Diagnose.** It snapshots every thread's state through the resource manager while the workload runs, reads who holds the contended mutex and its protocol directly from process memory, and applies a deterministic rule. Each finding lists its evidence, a confidence level and what it could not determine.
3. **Propose, approve, test.** The assistant proposes one exact source change, shown as a diff. Only a person can approve it, by typing "yes" at an interactive terminal; there is no tool for an assistant to approve itself, and the approval is tied to the diff's hash. qbridge then rebuilds with the QNX toolchain, redeploys and reruns the identical workload.
4. **Measure and verify.** It compares before and after, with misses and worst-case response first. It says "improved" only if every run after the change beat every run before it; otherwise "inconclusive". It writes a self-contained HTML report built only from recorded data, including what the measurements do not prove, and can roll back to the baseline binary, checked by sha256.

On the demo workload, a 10 ms control task with a planted priority inversion, the diagnosis found the inversion with high confidence. The approved fix took misses from 310/304/302 to 0/0/0 per 999 cycles, and the worst response from about 37 ms to 4.1–5.4 ms, over three runs each way on the target.

## How we built it

- **Target side: native QNX 8.0 C.**
  - `qbridged` is a resource manager (`resmgr_attach`, the `iofunc` layer with a custom OCB) serving `/dev/qbridge/*`.
  - Thread state comes from procfs (`DCMD_PROC_INFO`, `DCMD_PROC_TIDSTATUS`). The mutex holder and protocol come from the `sync_t` in the waiting process's memory (`/proc/<pid>/as`, `_NTO_SYNC_*` flags).
  - Process-death and core-dump events arrive as pulses (`procmgr_event_notify_add`, `DCMD_DUMPER_NOTIFYEVENT`, `MsgRegisterEvent`). Readers of `/dev/qbridge/events` stay blocked on a held reply until an event arrives.
  - Real-time tasks register once with a `devctl` and report every cycle with `MsgSendPulse` (`qbridge_task.h`), so they never block on the monitor. qbridged keeps their statistics in memory set aside at startup.
  - The demo workload uses `SCHED_FIFO` priorities, CPU run masks, `clock_nanosleep(TIMER_ABSTIME)` and `pthread_mutexattr_setprotocol`.
  - qbridged is built into the target image with `mkqnximage` snippets and starts at boot.
- **Host side: Python, standard library only.** An MCP server written directly on JSON-RPC over stdio, a CLI with the same tools, a build step with the SDP's `qcc` that returns structured compiler errors, statistics and miss attribution, the diagnosis rules, the experiment state machine, and an HTML report with inline SVG charts.
- **Safety.**
  - Read-only by default.
  - Human approval for every source change.
  - A command allowlist at the single point where commands leave for the target, with shell scripts only as fixed templates.
  - Everything confined to one deploy folder, with timeouts.
  - Rollback, and a hash-chained audit log of every action and refusal.
- **Testing.** 190 automated tests run against output recorded on the real target, so they need no target. Every feature was also run against the real target before it was called done.

## Challenges we ran into

- **The virtual machine's own timing.** The first version of the demo missed about a quarter of its deadlines even with the fix applied. We tested and ruled out two explanations, then built a timer-latency check (`vmjitter`). It showed that on this VirtualBox VM, timers on virtual CPU 0 fire 15–18 ms late on average and up to about 55 ms, even when idle, while CPU 1 stays within a few milliseconds. The demo now runs on CPU 1, and every report quotes the measured timer lateness.
- **Who holds a mutex.** The kernel reports which mutex a thread waits on, but not its owner. We read the mutex from the process's memory. The protocol flags are documented, but the owner field's layout is not, so we worked it out on the target, check it against the process ID on every read, and say so in every finding.
- **Message-passing details found only on the target.** A task's `close()` overtook its own last low-priority pulses, losing the final cycle. The rcvid of a client that gives up arrives in the unblock pulse's value, not where we first looked. Both were found by comparing qbridged's numbers with the task's own, and fixed.
- **Being honest about what a measurement proves.** We made the verdict strict ("improved" only if every run after beat every run before), kept execution time, latency and lock wait separate, and wrote the limits into every report.

## Accomplishments that we're proud of

**[your words]** Possible points: the complete loop working end to end on a real target; the three-way cross-check agreeing exactly; the diagnosis reading mutex ownership from process memory; human approval that an assistant cannot get around.

## What we learned

**[your words]** Possible points: QNX message passing and resource managers in practice; why measurement noise has to be measured before results can be trusted.

## What's next for qbridge RT

- Confirm diagnoses from kernel scheduler events with `tracelogger`.
- Run on real hardware and compare with the virtual machine.
- More rules: deadlocks and message-passing inversions are already detected in the general tools; next are priority-ceiling misuse and CPU overload.

## Built with

QNX SDP 8.0, QNX Neutrino RTOS 8.0, C, `qcc` (gcc 12.2), `mkqnximage`, VirtualBox, Python, Model Context Protocol (MCP), SVG.

## Links

- Repository: https://github.com/vychopade/qbridge (MIT licence)
- Demo video: **[YouTube link]**
- Build and run instructions: README, "Build and run"
- QNX-specific code: README, "How it uses QNX", and [docs/ARCHITECTURE.md](ARCHITECTURE.md)

## Limits stated in the submission

The measurements come from finite runs on a QNX 8.0 virtual machine. They show the effect of the fix on this workload; they do not prove a worst-case response time. rtdemo is an educational, simulated workload; nothing here is a claim about automotive, medical or other certified systems.
