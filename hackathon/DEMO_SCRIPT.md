# Demo video script (under 3 minutes)

Three faults, one loop. Everything shown is real output from the target. Where waiting time is cut or sped up, say so on screen. Never show numbers that were not produced on the target. Anything not produced during the recording itself gets an on-screen caption saying when it was run (for example "run before recording, same target").

Aim for **2:45-2:50**. Judges are not required to watch past 3:00, so a tight video is safer than a complete one.

## Rules for the video (from the hackathon rules)

- Public on YouTube, under 3 minutes, in English.
- It must show the project working on the device it was built for: the QNX 8.0 target. Keep the terminal output from the target visible, not only the assistant's words.
- **No third-party trademarks or copyrighted music** without permission. Keep logos out of frame or blur them: the MCP client's logo (for example Claude Code's), VirtualBox, Windows, GitHub. Plain text such as "MCP client" is fine. QNX's own name is fine (it's the sponsor's hackathon). Use your own voice, captions, or no music.

## Before recording

- [ ] VM running. In PowerShell, run these one at a time (no `--build`, so `/data` stays as it is):
  ```
  cd $env:USERPROFILE\qnx-target
  cmd /c "call %USERPROFILE%\qnx800\qnxsdp-env.bat && mkqnximage --run"
  ```
- [ ] `qbridge ps --process qbridged` shows qbridged running from `/system/bin/qbridged`.
- [ ] rtdemo and vmjitter are on the target (`qbridge build ...`, then `qbridge --allow-write deploy ...`; see the README, step 3).
- [ ] Repo source has both faults: `git status` is clean, and `examples\rtdemo\rtdemo.c` says `#define LOCK_PROTOCOL PTHREAD_PRIO_NONE` and `#define SERVER_CHANNEL_FLAGS _NTO_CHF_FIXED_PRIORITY`.
- [ ] Fresh timing checks: `qbridge --allow-write timing-check --cpu 1` and `--cpu 0`, so the reports quote today's numbers. If CPU 1's worst case is far above a few ms, the VM is noisy: restart the VM (and if needed the PC) and check again before recording.
- [ ] **Reports for the message-passing and overload scenarios, made before recording** (their experiments don't fit in the time). Either use the ones in `docs/` (example-report-msgpass.html, example-report-overload.html), or make fresh ones the same way as in the README. Open both in browser tabs.
- [ ] The MCP client with qbridge registered **with** `--allow-write`, its logo hidden or cropped.
- [ ] A **normal** PowerShell window, outside the editor, for the approval. The in-app terminal once failed to pass typing to the prompt.
- [ ] Font size large enough to read at 1080p; close other windows and notifications.
- [ ] Optional: use `--seconds 5 --repeats 2` for the live experiment to keep waiting short. If you do, say "5-second runs" in the voice-over.
- [ ] Dry run once with a stopwatch.

## Script

| Time | Screen | Voice-over (about) |
|---|---|---|
| **0:00-0:12** Problem | Caption: *"qbridge RT, built during the QNX Global Hackathon, Nov 2026. Running on QNX 8.0."* Terminal: `qbridge --allow-write run rtdemo -d 5` showing the JSON summary with `"misses"`. | "This is a simulated control task on a QNX 8.0 target. It must finish every 10 ms. Right now it misses about a third of its deadlines, and the code doesn't say why." |
| **0:12-0:25** Detect | In the assistant: *"rtdemo's control task is missing its 10 ms deadline on the QNX target. Find out why."* It calls `qnx_measure`. Show the summary: misses, worst response, "what started each run: waiting for a lock". | "qbridge RT measures every cycle on the target and splits it into scheduling latency, lock wait and CPU time. These misses start with long lock waits." |
| **0:25-0:50** Diagnose and confirm | `qnx_diagnose` with `trace: true` (sped up, say so). Show the finding (high confidence; A at 30 waits for a mutex held by C at 10 while B at 20 runs; PRIO_NONE), then "Confirmed by kernel trace" with its steps. | "It snapshots every thread through a QNX resource manager and reads who holds the mutex straight from the process's memory: a priority inversion. Then a kernel trace checks every step. Only then is it 'confirmed'." |
| **0:50-1:15** Propose, approve, test | `qnx_experiment_start`, then `qnx_experiment_propose` (show the one-line diff). Switch to PowerShell: `qbridge experiment approve <id>`, type **yes**. Back in the assistant: `qnx_experiment_run` (sped up, say so). | "It proposes a one-line fix, but it can't apply it. Only a person can approve, in a terminal, tied to this exact diff. Then it rebuilds with the QNX toolchain, redeploys and reruns the same workload." |
| **1:15-1:28** Before and after | The verdict and numbers: misses about 300 to 0, worst response about 37 ms to about 5 ms. | "Every run after the fix beat every run before it. It calls that 'improved', from a few runs on a virtual machine, not a proof." |
| **1:28-2:00** A QNX-only fault | The assistant, with only: *"Now run rtdemo -s msgpass. It misses deadlines too. Find out why and propose a fix."* Let it choose the tools: `qnx_measure` ("waiting for a server's reply"), `qnx_diagnose` with trace (sped up): "Priority inversion through message passing ... confirmed by kernel trace". Show its proposal: drop `_NTO_CHF_FIXED_PRIORITY`. Then the msgpass report tab (caption: "run before recording, same target"): 825/819/812 to 9/4/24. | "This one has no mutex. On QNX, services are separate processes, and a server normally works at the priority of the client asking it. This server's channel turns that off, so a medium-priority job holds up the reply. The assistant found that on its own, and the trace confirms it. The fix cut misses from over 800 to a handful; the report says the rest look like virtual-machine noise, and why." |
| **2:00-2:10** Third fault | The overload report tab (caption: "run before recording"): CPU preemption, fix `-a 15` to `-a 30`, misses about 300 to 0-1. | "A third fault, CPU overload with the wrong priorities, is fixed by a scheduling change with no code change, through the same approval and measurement." |
| **2:10-2:28** The QNX side | ssh to the target: `ls /dev/qbridge`, `cat /dev/qbridge/deadlines`. Briefly show `target/qbridged/qbridge_task.h` (`qbt_report`, `MsgSendPulse`). | "On the target, a native resource manager serves thread state and deadline statistics as files. The control task reports every cycle with a QNX pulse, so it never blocks on the monitor." |
| **2:28-2:38** Keep it fixed | Terminal: `qbridge --allow-write gate rtdemo --repeats 2 --seconds 5` showing `FAIL` and the exit code (sped up). | "And to keep it fixed, the same measurement runs as a pass-or-fail check a CI job can use." |
| **2:38-2:50** Close | The report's Limitations section (timer lateness per CPU), then the project site. | "Every report is built only from recorded data, including what the measurements don't prove. qbridge RT: find out why a real-time task missed its deadline, test a fix, and measure it." |

To ssh to the target in the 2:10 beat:

```
ssh -i %USERPROFILE%\.ssh\qnx_target_ed25519 -o MACs=hmac-sha2-256 root@<target address>
ls /dev/qbridge
cat /dev/qbridge/deadlines
```

`qbridge info` shows the target address.

If a beat runs long, cut in this order: the gate beat, then "The QNX side" voice-over. Keep the approval, the before/after numbers and the message-passing beat.

## After recording

- [ ] `qnx_experiment_rollback` (or `qbridge --allow-write experiment rollback <id>`) so the repo keeps the fault. Check that `git status` is clean.
- [ ] Watch the whole video once for logos and readable text.
- [ ] Upload to YouTube as **public**, under 3 minutes, and put the link in the submission, the README's judges' path and `docs/index.html`.

## Things not to say

- That a fix is proven or the task is now guaranteed to meet its deadline. Say "in these runs".
- That the message-passing fix removed every miss. It didn't: 9, 4 and 24 misses remained, and the VM explanation is likely but not proven.
- That the kernel trace proves the cause in general. It shows each step happened in that one-second window.
- Anything about real vehicles, medical devices or certification. This is a simulated, educational demo.
- That ssh access or gdb is the novel part. The novel parts are the measured experiment loop, the evidence-based diagnosis confirmed by a kernel trace, the QNX-specific message-passing rule, and the native QNX resource manager with pulse-based reporting.
