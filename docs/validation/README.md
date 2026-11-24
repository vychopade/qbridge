# Validating qbridge's .kev decoder against QNX traceprinter

qbridge decodes raw QNX 8.0 kernel traces (`.kev` files from `tracelogger`) itself, so a trace can be analysed on any computer without the QNX SDP ([src/qbridge/parsers/kevbin.py](../../src/qbridge/parsers/kevbin.py)). This folder holds the evidence that it decodes them the way QNX's own `traceprinter` does.

**Result** ([kev-validation.json](kev-validation.json), Nov 24, 2026): **1,236,591 real events in 36 traces, every one matched by traceprinter, 0 mismatches.**

| | |
|---|---|
| Events examined (as printed by traceprinter) | 1,236,591 |
| Paired with a qbridge event (same timestamp, CPU, class, event) | 1,236,591 |
| Missing in qbridge / extra in qbridge | 0 / 0 |
| Events whose fields qbridge decodes ("supported") | 1,217,251 |
| ... all fields equal to traceprinter's | 1,217,251 |
| Fields compared / equal | 3,354,492 / 3,354,492 |
| 64-bit timestamps checked / equal | 1,236,591 / 1,236,591 |
| Events out of traceprinter's order on their CPU | 0 |
| Events matched on identity, time and CPU only (fields not decoded) | 19,340 |
| Files that failed to parse | 0 |

## The traces

All recorded on the QNX 8.0 reference target (VirtualBox, x86_64, 2 CPUs) with the SDP's `tracelogger`, while [rtdemo](../../examples/rtdemo/README.md) ran. Every process on the target is in them, not only rtdemo.

| Set | Files | Events | Classes | Notes |
|---|---|---|---|---|
| Captures made by `qbridge trace` and `qbridge diagnose --trace`, Nov 22–23 | 21 | 89,753 | thread, process, system (wide) | the traces behind every recorded result in this repository |
| 0.5 s captures of the four scenarios, Nov 24 | 8 | 570,970 | all (wide and fast) | the kernel dropped 1,177,711 events (`CONTROL DROPPED`): useful as damaged input |
| 0.25 s captures, fault and fix of each scenario, Nov 24 | 7 | 575,868 | all (wide, one fast) | no drops; shrunk copies are the test fixtures |

Each file is listed in the report with its SHA-256. The traces themselves stay outside the repository (about 25 MB). The shrunk copies in [tests/fixtures/kev](../../tests/fixtures/kev/README.md) and the raw files in [examples/traces](../../examples/traces/README.md) are checked against traceprinter on every CI run.

**What dominates.** rtdemo's busy-wait calls `ClockTime`, so 87% of the events are `ClockTime` kernel call entries and exits. Without them, 157,389 events of 100 other types remain, all matched. The report's `by_type` gives every type's count, and the table below gives the most frequent ones.

| Event type | Events | Fields equal / compared |
|---|---|---|
| THREAD:THRUNNING | 37,121 | 142,914 / 142,914 |
| THREAD:THREADY | 33,144 | 127,902 / 127,902 |
| THREAD:THINTR | 10,026 | 38,594 / 38,594 |
| THREAD:THRECEIVE | 8,566 | 32,868 / 32,868 |
| THREAD:THCREATE | 4,483 | 8,966 / 8,966 |
| PROCESS:PROCTHREAD_NAME | 3,202 | 9,606 / 9,606 |
| THREAD:THREPLY | 3,122 | 12,158 / 12,158 |
| COMM:REC_PULSE, SND_PULSE | 4,222 | 8,444 / 8,444 |
| CONTROL:TIME, BUFFER | 4,312 | 8,624 / 8,624 |
| THREAD:THCONDVAR, THMUTEX, THSEM, THSEND | 4,704 | 18,098 / 18,098 |
| COMM:SND_MESSAGE, REC_MESSAGE, REPLY_MESSAGE | 2,243 | 4,486 / 4,486 |
| KER_CALL/EXIT:MSG_SENDV, MSG_RECEIVEV, MSG_REPLYV | see report | all equal |
| INTERRUPT:INT_DELIVER | 1,336 | 1,336 / 1,336 |

## Method

`qbridge validate-kev` ([src/qbridge/kevvalidate.py](../../src/qbridge/kevvalidate.py)) decodes each `.kev` natively and parses traceprinter's text for the same file independently (it does not reuse qbridge's own text parser for pairing). Then:

1. **Identity.** Events are paired by what traceprinter prints on each line: the low 32 bits of the timestamp, the CPU, the class and the event (kernel calls by number, because traceprinter shortens some exit names, e.g. `SYNC_CONDVAR_SIG/83`). An event either side has without a partner is a mismatch.
2. **Fields.** For each pair, every field that both decoders print is compared by value. Hex and decimal, signed and unsigned forms of the same word count as equal. traceprinter's `msg[0]:"..." (0x.. 0x.. 0x..)` is split into its three words.
3. **Time.** qbridge's full 64-bit timestamps are compared with a reconstruction from traceprinter's text: the `msb` of each `CONTROL TIME` plus the rollovers of the low 32 bits.
4. **Order.** On each CPU, qbridge's order of the paired events is compared with traceprinter's.

An event is "supported" when qbridge decodes its fields from the documented layout. Other events keep their class, name, time, CPU and raw data words. They are counted as matched on identity, time and CPU only, never as decoded.

## What is not decoded, and why

The layouts come from the SDP's own documentation: `<sys/trace.h>`, `<sys/states.h>` and the System Analysis Toolkit's event table ([tools/gen_kevspec.py](../../tools/gen_kevspec.py) generates [kevspec.py](../../src/qbridge/parsers/kevspec.py) from it). Where that table does not describe what QNX 8.0 writes, the fields are left raw instead of guessed:

- **SYSTEM class** (IPI, TIMER, SLOG2, PATHMGR_OPEN, MMAP, IST_ATTACH, ...): no field layouts in the table. Name, time and CPU only.
- **`MsgReceivePulsev`, `SyncTypeDestroy`, `TimerTimeout` entry**, and **`MsgReceivev` exit and `TraceEvent` entry in wide mode**: the table's layout does not match the size of what QNX 8.0 logs (traceprinter prints fields such as `info->nd` and `cmd/param1/param2` that the table does not list).
- **`ThreadCreate` entry**: qbridge's values follow the documented layout and look right (policy 2, priority 21, guard size 4096), but traceprinter prints them one 32-bit word later. The two disagree, so qbridge leaves this event undecoded (`kevbin.DISPUTED`).
- **`SyncMutexUnlock` in fast mode on 64-bit**: the documented fast layout (64-bit `sync_p` plus `owner`) does not fit one slot.

Nothing here is needed by qbridge's analysis. Thread states, names, mutex, condvar and semaphore calls, message passing and communication events are all decoded and validated.

## Limits

- One machine type: x86_64, little-endian, 2 CPUs. Big-endian decoding is implemented and tested on synthetic files only. Real big-endian or ARM traces have not been checked.
- One QNX release (8.0.0, kernel 2.8.0) and one traceprinter (1.02).
- Event types not in these traces (for example USER events, QVM, PMU, security, APS) are decoded by name only, or not at all, and are not validated.
- The comparison checks qbridge against traceprinter. Where both are wrong in the same way, it cannot tell.

## Rerunning it

On a computer with the QNX 8.0 SDP and a target running the instrumented kernel (`procnto-smp-instr`):

```
# on the target: any workload, any classes; -w for wide events
tracelogger -w -n 0 -s 1 -f /tmp/mytrace.kev

# on the host
scp root@<target>:/tmp/mytrace.kev .
qbridge validate-kev mytrace.kev --out report.json
```

`validate-kev` runs the SDP's traceprinter itself when no `<name>.txt` or `<name>.traceprinter.txt` sits beside the file. It exits non-zero on any mismatch. Without the SDP, it checks against saved traceprinter output only:

```
qbridge validate-kev tests/fixtures/kev/*.kev.gz
```
