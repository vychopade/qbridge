# Raw kernel trace fixtures

Real `.kev` files from the QNX 8.0 reference target (VirtualBox, 2 CPUs), recorded on Nov 24, 2026 with

```
tracelogger -w -k 128 -n 0 -s 0.25 -f <file>        # overload_fast: without -w (fast mode)
```

while `rtdemo` ran (`-n -d 3`, tracing from 1.2 s in). Every class is on: kernel calls, interrupts, process, thread, communication and system events. 128 buffers per CPU meant no events were dropped (no `CONTROL DROPPED` in any of these files).

| File | Workload |
|---|---|
| `inversion_fault` | `rtdemo -s inversion` (mutex with `PTHREAD_PRIO_NONE`) |
| `inversion_fixed` | the same with `PTHREAD_PRIO_INHERIT` (rebuilt with `LOCK_PROTOCOL` changed) |
| `msgpass_fault` | `rtdemo -s msgpass` (server channel with `_NTO_CHF_FIXED_PRIORITY`) |
| `msgpass_fixed` | the same with the channel flag removed (rebuilt with `SERVER_CHANNEL_FLAGS 0`) |
| `condvar_fault` | `rtdemo -s condvar` (producer at 10, below B-worker) |
| `condvar_fixed` | `rtdemo -s condvar -p 25` |
| `overload_fast` | `rtdemo -s overload -a 15`, fast mode |

**Shrunk:** rtdemo's busy-wait calls `ClockTime` hundreds of thousands of times a second, which made each file 2–3 MB. `tools/shrink_kev.py` removed exactly those kernel-call slots (call 65) and rewrote each buffer's slot count. Nothing else was changed. The full files are kept outside the repository; the validation report in `docs/validation/` covers them.

`<name>.traceprinter.txt.gz` is what the SDP's `traceprinter` (QNX 8.0) prints for exactly the shrunk `<name>.kev.gz`. `tests/test_kevbin.py` checks qbridge's own decoder against it, event by event, on every CI run.
