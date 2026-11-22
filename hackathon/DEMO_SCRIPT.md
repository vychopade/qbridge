# Demo video script (under 3 minutes)

Two scenarios, one loop. Everything shown is real output from the target. Where waiting time is cut or sped up, say so on screen. Never show numbers that were not produced on the target; anything not produced during the recording itself gets an on-screen caption saying when it was run (for example "run before recording, same target").

## Before recording

- [ ] VM running: `pushd %USERPROFILE%\qnx-target && mkqnximage --run && popd` (no `--build`, so `/data` stays as it is).
- [ ] `qbridge ps --process qbridged` shows qbridged running from `/system/bin/qbridged`.
- [ ] rtdemo and vmjitter are on the target (`qbridge build ...`, then `qbridge --allow-write deploy ...`; see the README, step 3).
- [ ] Repo source has the fault: `git status` is clean and `examples\rtdemo\rtdemo.c` says `#define LOCK_PROTOCOL PTHREAD_PRIO_NONE`.
- [ ] Fresh timing checks: `qbridge --allow-write timing-check --cpu 1` and `--cpu 0`, so the reports quote today's numbers. If CPU 1's worst case is far above a few ms, the VM is in a noisy state: restart the VM (and if needed the PC) and check again before recording.
- [ ] **Second scenario, run before recording** (its experiment would not fit into 20 seconds). In a normal terminal:
  ```
  qbridge --allow-write diagnose rtdemo --seconds 8 --trace -- -s overload -a 15
  qbridge --allow-write experiment start rtdemo --project examples\rtdemo --repeats 3 -- -s overload -a 15
  qbridge experiment propose <id> --args "-s overload -a 30" --diagnosis-id <diagnosis id> --description "Run the control task above the CPU-heavy worker"
  qbridge experiment approve <id>
  qbridge --allow-write experiment run <id>
  qbridge report <id>
  qbridge --allow-write experiment rollback <id>
  ```
  Keep that report open in a browser tab for the 1:55 beat.
- [ ] Claude Code (or another MCP client) with qbridge registered **with** `--allow-write`.
- [ ] A **normal** PowerShell or Command Prompt window, outside the editor, for the approval. The in-app terminal once failed to pass typing to the prompt.
- [ ] Font size large enough to read at 1080p; close other windows.
- [ ] Optional dry run with `--seconds 5 --repeats 2` to keep waiting short. If you use it, say "5-second runs" in the voice-over.

## Script

| Time | Screen | Voice-over (about) |
|---|---|---|
| **0:00–0:15** Problem | `examples/rtdemo/rtdemo.c` header comment, then the terminal: `qbridge --allow-write run rtdemo -d 5` showing the JSON summary with `"misses"` | "This is a simulated control task on a QNX 8.0 target. It must finish every 10 ms. Right now it misses about a third of its deadlines, and the code doesn't say why." |
| **0:15–0:35** Detect | In the assistant: *"rtdemo's control task is missing its 10 ms deadline on the QNX target. Find out why."* It calls `qnx_measure`. Show the summary line: misses, worst response, "what started each run: waiting for a lock". | "qbridge RT measures every cycle on the target and splits it into scheduling latency, lock wait and CPU time. These misses start with long lock waits." |
| **0:35–1:05** Diagnose and confirm | The assistant calls `qnx_diagnose` with `trace: true` (sped up, say so). Show the finding (high confidence; A at 30 waits for a mutex held by C at 10 while B at 20 runs; the PRIO_NONE flag), then "Confirmed by kernel trace: every step was seen" with the four steps. | "It snapshots every thread through a QNX resource manager and reads who holds the mutex straight from the process's memory: a priority inversion. Then it records a kernel trace with tracelogger and checks every step: the waiter blocked, the holder stuck at low priority, the middle thread running. Only then is it 'confirmed'." |
| **1:05–1:35** Propose, approve, test | The assistant calls `qnx_experiment_start`, then `qnx_experiment_propose` (show the one-line diff). Switch to the PowerShell window: `qbridge experiment approve <id>`, show the diff, type **yes**. Back in the assistant: `qnx_experiment_run` (sped up, say so). | "It proposes a one-line fix, but it can't apply it. Only a person can approve, in a terminal, and the approval is tied to this exact diff. Then it rebuilds with the QNX toolchain, redeploys and reruns the same workload." |
| **1:35–1:55** Before and after | The verdict and the before/after numbers: misses about 300 to 0, worst response about 37 ms to about 5 ms. | "Every run after the fix beat every run before it. It calls that 'improved', from a few runs on a virtual machine, not a proof." |
| **1:55–2:15** Second scenario | The assistant: *"Now try the overload scenario: rtdemo -s overload -a 15."* It calls `qnx_diagnose` with those args and `trace: true` (sped up): "CPU preemption ... confirmed by kernel trace". Then the browser tab with the report made before recording (caption: "run before recording, same target"): the trace timeline, and the verdict for the priority change 15 to 30. | "A different fault: no lock this time. The control task is ready, but a higher-priority worker holds its CPU, and the trace shows it. The fix is a scheduling change, not a code change, and it goes through the same approval and measurement." |
| **2:15–2:35** The QNX side | ssh to the target: `ls /dev/qbridge`, then `cat /dev/qbridge/deadlines`. Briefly show `target/qbridged/qbridge_task.h` (`qbt_report` and `MsgSendPulse`). | "On the target, a native resource manager serves thread state and deadline statistics as files. The control task reports every cycle with a QNX pulse, so it never blocks on the monitor." |
| **2:35–2:55** Report and close | `qnx_report`, then open the HTML: verdict, charts, the trace check, the Limitations section (timer lateness per CPU). Then `qnx_experiment_rollback`. | "Everything ends up in a report built only from recorded data, including what the measurements don't prove. qbridge RT: find out why a real-time task missed its deadline, test a fix, and measure it." |

To ssh to the target in the 2:15 beat:

```
ssh -i %USERPROFILE%\.ssh\qnx_target_ed25519 -o MACs=hmac-sha2-256 root@<target address>
ls /dev/qbridge
cat /dev/qbridge/deadlines
```

`qbridge info` shows the target address.

If a beat runs long, shorten the voice-over of "The QNX side" first; keep the approval and the before/after numbers.

## After recording

- [ ] `qnx_experiment_rollback` (or `qbridge --allow-write experiment rollback <id>`) so the repo keeps the fault. Check that `git status` is clean.
- [ ] Upload to YouTube as **public**, under 3 minutes, and put the link in the submission.

## Things not to say

- That the fix is proven or the task is now guaranteed to meet its deadline. Say "in these runs".
- That the kernel trace proves the cause in general. It shows each step happened in that one-second window.
- Anything about real vehicles, medical devices or certification. This is a simulated, educational demo.
- That ssh access or gdb is the novel part. The novel parts are the measured experiment loop, the evidence-based diagnosis confirmed by a kernel trace, and the native QNX resource manager with pulse-based reporting.
