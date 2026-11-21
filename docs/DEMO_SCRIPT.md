# Demo video script (under 3 minutes)

Follows the plan's seven beats. Everything shown is real output from the target. Where waiting time is cut or sped up, say so on screen. Never show numbers that were not produced in that recording.

## Before recording

- [ ] VM running: `pushd %USERPROFILE%\qnx-target && mkqnximage --run && popd` (no `--build`, so `/data` stays as it is).
- [ ] `qbridge ps --process qbridged` shows qbridged running from `/system/bin/qbridged`.
- [ ] rtdemo and vmjitter are on the target (`qbridge build ...`, then `qbridge --allow-write deploy ...`; see the README, step 3).
- [ ] Repo source has the fault: `git status` is clean and `examples\rtdemo\rtdemo.c` says `#define LOCK_PROTOCOL PTHREAD_PRIO_NONE`.
- [ ] Fresh timing checks: `qbridge --allow-write timing-check --cpu 1` and `--cpu 0`, so the report quotes today's numbers.
- [ ] Claude Code (or another MCP client) with qbridge registered **with** `--allow-write`.
- [ ] A **normal** PowerShell or Command Prompt window, outside the editor, for the approval. The in-app terminal once failed to pass typing to the prompt.
- [ ] Font size large enough to read at 1080p; close other windows.
- [ ] Optional dry run with `--seconds 5 --repeats 2` to keep waiting short. If you use it, say "5-second runs" in the voice-over.

## Script

| Time | Screen | Voice-over (about) |
|---|---|---|
| **0:00–0:20** Problem | `examples/rtdemo/rtdemo.c` header comment, then the terminal: `qbridge --allow-write run rtdemo -d 5` showing the JSON summary with `"misses"` | "This is a simulated control task on a QNX 8.0 target. It must finish every 10 ms. Right now it misses about a third of its deadlines, and the code doesn't say why." |
| **0:20–0:45** Detect | In the assistant: *"rtdemo's control task is missing its 10 ms deadline on the QNX target. Find out why."* It calls `qnx_measure`. Show the summary line: misses, worst response, "what started each run: waiting for a lock". | "qbridge RT measures every cycle on the target and splits it into scheduling latency, lock wait and CPU time. The misses start with long lock waits. Its numbers are cross-checked against the task's own and against the QNX daemon's." |
| **0:45–1:15** Diagnose | The assistant calls `qnx_diagnose`. Show the finding: high confidence; A at 30 waits for a mutex held by C at 10 while B at 20 runs; the PRIO_NONE flag; the "could not determine" list. | "It snapshots every thread's state through a QNX resource manager, reads who holds the mutex straight from the process's memory, and finds a priority inversion: the lock was created without priority inheritance. It says how sure it is, and what it can't know." |
| **1:15–1:50** Propose, approve, test | The assistant calls `qnx_experiment_start`, then `qnx_experiment_propose` (show the one-line diff). Switch to the PowerShell window: `qbridge experiment approve <id>`, show the diff, type **yes**. Back in the assistant: `qnx_experiment_run` (sped up, say so). | "It proposes a one-line fix, but it can't apply it. Only a person can approve, in a terminal, and the approval is tied to this exact diff. Then it rebuilds with the QNX toolchain, redeploys and reruns the same workload." |
| **1:50–2:20** Before and after | The verdict and the before/after numbers: misses about 300 to 0, worst response about 37 ms to about 5 ms. | "Every run after the fix beat every run before it: no missed deadlines, and the worst case dropped from 37 to about 5 milliseconds. It calls that 'improved', and says it's from a few runs on a virtual machine, not a proof." |
| **2:20–2:40** The QNX side | ssh to the target: `ls /dev/qbridge`, then `cat /dev/qbridge/deadlines`. Briefly show `target/qbridged/qbridge_task.h` (`qbt_report` and `MsgSendPulse`). | "On the target, a native resource manager serves thread state and deadline statistics as files. The control task reports every cycle with a QNX pulse, so it never blocks on the monitor." |
| **2:40–3:00** Report and close | `qnx_report`, then open the HTML: verdict, charts, the Limitations section (timer lateness per CPU). Then `qnx_experiment_rollback`. | "Everything ends up in a report built only from recorded data, including what the measurements don't prove. qbridge RT: find out why a real-time task missed its deadline, test a fix, and measure it." |

To ssh to the target in the 2:20 beat:

```
ssh -i %USERPROFILE%\.ssh\qnx_target_ed25519 -o MACs=hmac-sha2-256 root@<target address>
ls /dev/qbridge
cat /dev/qbridge/deadlines
```

`qbridge info` shows the target address.

## After recording

- [ ] `qnx_experiment_rollback` (or `qbridge --allow-write experiment rollback <id>`) so the repo keeps the fault. Check that `git status` is clean.
- [ ] Upload to YouTube as **public**, under 3 minutes, and put the link in the submission.

## Things not to say

- That the fix is proven or the task is now guaranteed to meet its deadline. Say "in these runs".
- Anything about real vehicles, medical devices or certification. This is a simulated, educational demo.
- That ssh access or gdb is the novel part. The novel parts are the measured experiment loop, the evidence-based diagnosis, and the native QNX resource manager with pulse-based reporting.
