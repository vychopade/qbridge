"""MCP server for qbridge, over stdio, with no third-party dependencies.

MCP's stdio transport is JSON-RPC 2.0, one message per line. qbridge needs
only the server side of: initialize, ping, tools/list and tools/call.
Logging goes to stderr; stdout carries protocol messages only.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

from . import __version__, audit, experiments, tools
from .policy import Policy, PolicyError
from .qnxenv import QnxEnvError
from .target import Target, TargetError

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "qbridge RT connects you to a QNX 8.0 target to find out why a real-time task misses its "
    "deadline, test a fix and measure it. Typical order: qnx_timing_check (how noisy the "
    "target's timers are), qnx_measure (deadline statistics, misses attributed to lock wait, "
    "latency, CPU time), qnx_diagnose (evidence-based findings with confidence; trace=true checks each finding against a kernel trace from the same run), "
    "qnx_experiment_start, qnx_experiment_propose (one exact change), then ask the user to "
    "approve it by running 'qbridge experiment approve <id>' in a terminal (you cannot approve "
    "it), qnx_experiment_run (rebuild, rerun, compare), qnx_report, and qnx_experiment_rollback "
    "or _keep. Report results as measured: finite runs do not prove a worst case, and say "
    "'inconclusive' when the tools do. General tools: qnx_trace (what each thread did, from a short kernel trace), qnx_trace_file (the same for a trace file someone recorded, with no target), qnx_ps and qnx_logs (what is running, "
    "who is blocked on whom), qnx_crash (backtrace from a core file), qnx_build. Tools that "
    "change the target only work if the user started qbridge with --allow-write; every call is "
    "recorded in an audit log (qnx_audit)."
)


def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [],
            "additionalProperties": False}


# name -> (description, input schema, handler(session, **args), changes_target)
TOOLS: dict[str, tuple[str, dict, Callable[..., dict], bool]] = {
    "qnx_info": (
        "Target address, QNX version, CPUs and memory, and whether write tools are enabled.",
        _schema({}),
        lambda s: tools.info(s),
        False,
    ),
    "qnx_ps": (
        "List processes and threads on the QNX target with priority, scheduling policy and "
        "state, plus a blocked-on graph (which process each SEND/REPLY-blocked thread is "
        "waiting on) and any cycles in it.",
        _schema({"process": {"type": "string", "description": "Name substring or pid to filter by"}}),
        lambda s, process=None: tools.ps(s, process),
        False,
    ),
    "qnx_logs": (
        "Read the QNX system log (slog2). Filter by process, pid, time, minimum severity or text.",
        _schema({
            "process": {"type": "string", "description": "Process / buffer set name substring"},
            "pid": {"type": "integer"},
            "since": {"type": "number", "description": "Only entries at or after this epoch time"},
            "severity": {"type": "string", "description": "Minimum severity",
                         "enum": ["shutdown", "critical", "error", "warning", "notice", "info",
                                  "debug1", "debug2"]},
            "grep": {"type": "string", "description": "Message contains this text"},
            "limit": {"type": "integer", "description": "Newest N entries (default 200)"},
        }),
        lambda s, **a: tools.logs(s, **a),
        False,
    ),
    "qnx_build": (
        "Build QNX code on the host: a folder with a Makefile (runs make), or one C/C++ file "
        "(runs qcc for x86_64). Returns errors and warnings with file, line and column.",
        _schema({
            "path": {"type": "string", "description": "Folder with a Makefile, or a source file"},
            "make_target": {"type": "string"},
            "output": {"type": "string", "description": "Output path when building one file"},
        }, ["path"]),
        lambda s, path, make_target=None, output=None: tools.build(s, path, make_target, output),
        False,
    ),
    "qnx_deploy": (
        "Copy a host file (usually a built binary) to the target's deploy folder and make it "
        "executable. Needs write access.",
        _schema({
            "local_path": {"type": "string"},
            "name": {"type": "string", "description": "Name or path under the deploy folder"},
        }, ["local_path"]),
        lambda s, local_path, name=None: tools.deploy(s, local_path, name),
        True,
    ),
    "qnx_run": (
        "Run a deployed program on the target and return its output and exit code. Only "
        "programs in the deploy folder can be run. With background=true it returns at once "
        "and the program keeps running (output to <program>.log) so you can inspect it live "
        "with qnx_ps; it is still stopped after `timeout` seconds. Needs write access.",
        _schema({
            "path": {"type": "string", "description": "Name or path under the deploy folder"},
            "args": {"type": "array", "items": {"type": "string"}},
            "timeout": {"type": "integer",
                        "description": "Seconds before it is stopped (default 30; background max 3600)"},
            "background": {"type": "boolean", "description": "Start it and return immediately"},
        }, ["path"]),
        lambda s, path, args=None, timeout=30, background=False:
            tools.run(s, path, args, timeout, background),
        True,
    ),
    "qnx_kill": (
        "Send a signal to programs qbridge deployed (never to system processes), by pid or "
        "program name. Returns their log output. Signal ABRT makes dumper write a core file, "
        "so qnx_crash can show where a hung or deadlocked program was stuck. Needs write access.",
        _schema({
            "pid": {"type": "integer"},
            "name": {"type": "string", "description": "Program name, e.g. 'deadlock'"},
            "signal": {"type": "string", "enum": sorted(tools.KILL_SIGNALS),
                       "description": "Default TERM"},
        }),
        lambda s, pid=None, name=None, signal="TERM": tools.kill(s, pid, name, signal),
        True,
    ),
    "qnx_measure": (
        "Run a periodic real-time workload (e.g. rtdemo) on the target for a fixed time and "
        "report deadline statistics computed from its per-cycle samples: misses, worst/p99/"
        "mean response, and the separate parts of each cycle (backlog, scheduling latency, "
        "lock wait, CPU time, preempted time). Consecutive misses are grouped and each group "
        "is attributed to what started it. The run is saved on the host (run_id) for later "
        "comparison. Timing on a virtual machine is noisy; results describe these runs only. "
        "Needs write access.",
        _schema({
            "program": {"type": "string", "description": "Deployed workload, e.g. 'rtdemo'"},
            "seconds": {"type": "integer", "description": "Run length, 1-600 (default 10)"},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "Extra arguments for the program"},
            "label": {"type": "string", "description": "Name for the saved run, e.g. 'baseline'"},
            "deadline_us": {"type": "number", "description": "Override the program's deadline"},
        }, ["program"]),
        lambda s, program, seconds=10, args=None, label=None, deadline_us=None:
            tools.measure(s, program, seconds, args, label, deadline_us),
        True,
    ),
    "qnx_gate": (
        "Regression check for CI against a lab target: measure a workload `repeats` times "
        "(like qnx_measure) and pass or fail it. Fails if any run has more than max_misses "
        "misses, a worst response above max_worst_ms, or (with baseline run ids) more misses or "
        "a worse worst response than the worst baseline run. Passing means these runs met the "
        "limits, not that the deadline can never be missed. Needs write access.",
        _schema({
            "program": {"type": "string", "description": "Deployed workload, e.g. 'rtdemo'"},
            "repeats": {"type": "integer", "description": "Runs to measure, 1-10 (default 3)"},
            "seconds": {"type": "integer", "description": "Length of each run (default 10)"},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "Extra arguments for the program"},
            "max_misses": {"type": "integer", "description": "Most misses allowed per run "
                           "(default 0)"},
            "max_worst_ms": {"type": "number", "description": "Highest worst response allowed"},
            "baseline": {"type": "array", "items": {"type": "string"},
                         "description": "Saved run ids no run may be worse than"},
        }, ["program"]),
        lambda s, program, repeats=3, seconds=10, args=None, max_misses=0, max_worst_ms=None,
               baseline=None: tools.gate(s, program, repeats, seconds, args, max_misses,
                                         max_worst_ms, baseline),
        True,
    ),
    "qnx_deadlines": (
        "Live deadline statistics kept by qbridged on the target for every task that registered "
        "with it (tasks report each cycle by QNX message passing). Read-only: works for tasks "
        "you started yourself. Per task: cycles, misses, and max/mean/p99 of response, backlog, "
        "scheduling latency, lock wait and CPU time.",
        _schema({}),
        lambda s: tools.deadline_stats(s),
        False,
    ),
    "qnx_diagnose": (
        "Find out why a real-time task misses deadlines. Takes /dev/qbridge/threads snapshots "
        "while the workload runs (starting it if `program` is given) and applies deterministic "
        "rules: priority inversion on a mutex (reads the mutex's holder and protocol from process "
        "memory), priority inversion through QNX message passing (the deadline task is SEND- or "
        "REPLY-blocked on a server process running below it, for example a channel created with "
        "_NTO_CHF_FIXED_PRIORITY, while a middle-priority thread has the server's CPU), priority "
        "inversion on a condition variable or semaphore (the task waits in CONDVAR/SEM while a "
        "lower-priority thread of its process, the likely signaller, is held off the CPU by a "
        "middle-priority thread; medium confidence at most, as no owner is recorded) and CPU "
        "preemption (the deadline task is ready but a higher-priority thread holds its CPU); when "
        "no other thread explains the misses, also own work overrun (cycles that used far more "
        "CPU time than usual) and late wake-up (the task started late with nothing ahead of it, "
        "compared with the saved timer-lateness check). "
        "Each finding lists its evidence with numbers, a confidence level, what "
        "it could not determine, and a suggested fix. With trace=true it also records a short "
        "kernel trace (tracelogger) in the same run and checks each step of each finding against "
        "the thread state changes; a finding is 'confirmed' only if every step is seen, otherwise "
        "the unsupported steps are listed. Starting a program or tracing needs write access.",
        _schema({
            "program": {"type": "string", "description": "Workload to start, e.g. 'rtdemo'"},
            "seconds": {"type": "integer", "description": "Workload length, 3-60 (default 6)"},
            "snapshots": {"type": "integer", "description": "Max snapshots (default 40)"},
            "run_id": {"type": "string", "description": "Use this saved run's deadline report"},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "Extra workload arguments, e.g. ['-s', 'overload', '-a', '15']"},
            "trace": {"type": "boolean", "description": "Confirm findings with a kernel trace"},
            "trace_seconds": {"type": "number", "description": "Trace length, up to 5 s (default 1)"},
        }),
        lambda s, program=None, seconds=6, snapshots=40, run_id=None, args=None, trace=False,
               trace_seconds=1.0:
            tools.diagnose(s, program, seconds, snapshots, run_id, args, trace, trace_seconds),
        True,    # starts the workload when `program` is given
    ),
    "qnx_trace": (
        "Record a short QNX kernel trace (tracelogger on the instrumented kernel; thread, process "
        "and system events) and decode it on the host with traceprinter. Reports, per thread, the "
        "time spent in each state (RUNNING, READY, MUTEX, NANOSLEEP, ...) and, for its longest "
        "waits, which threads ran on its CPU instead and at what priority. Optionally starts a "
        "workload first. Raw files stay in the qbridge home folder. Needs write access.",
        _schema({
            "program": {"type": "string", "description": "Workload to start first, e.g. 'rtdemo'"},
            "args": {"type": "array", "items": {"type": "string"}},
            "seconds": {"type": "integer", "description": "Workload length (default 6)"},
            "trace_seconds": {"type": "number", "description": "Trace length, up to 5 s (default 1)"},
            "threads": {"type": "array", "items": {"type": "string"},
                        "description": "Thread names to report (default: the program's threads)"},
        }),
        lambda s, program=None, args=None, seconds=6, trace_seconds=1.0, threads=None:
            tools.trace(s, program, args, seconds, trace_seconds, threads),
        True,
    ),
    "qnx_trace_file": (
        "Analyse a RECORDED QNX kernel trace on the host; no target needed. Input: a raw .kev "
        "file from tracelogger (qbridge decodes it itself; no QNX SDP needed), or traceprinter "
        "text (also .txt.gz). Works on "
        "any program's trace. Reports, per thread, time in each state, ready-queue waits (p50, "
        "p99, max), and for its longest waits who held its CPU (READY) or which lower-priority "
        "threads ran meanwhile (MUTEX/SEND/REPLY). For periodic threads it rebuilds every cycle "
        "from the trace and gives deadline statistics (period found from the wake-ups, deadline = "
        "period unless given), with late wake-up and overrun findings. Flags patterns as hints, not diagnoses: CPU "
        "preemption, and possible priority inversion (a thread blocked while a lower-priority "
        "thread was held off its CPU by a middle-priority one). If the trace has kernel-call and "
        "communication events, 'dependencies' ties each mutex, message, condvar and semaphore "
        "wait to the thread that ended it (SyncMutexUnlock of the same mutex, the server's "
        "reply, the signal), across processes and CPUs, and classifies it: "
        "confirmed_priority_inversion, probable_scheduling_interference, no_inversion or "
        "insufficient_evidence, with the events (time, CPU, thread, file offset) as evidence. "
        "Quote that evidence; never name a holder the result does not. Without those events "
        "the patterns are only probable: say 'possible', and qnx_diagnose with trace=true "
        "identifies the holder on a live target. Also writes an HTML page with a thread-state "
        "timeline, and with perfetto=true a file the user can open in ui.perfetto.dev (arrows "
        "from each waiter to its holder); returns their paths.",
        _schema({
            "path": {"type": "string", "description": "Path of the trace file on this computer"},
            "threads": {"type": "array", "items": {"type": "string"},
                        "description": "Thread names or 'pid/tid' to report (default: threads "
                                       "with a pattern, then the longest waits)"},
            "process": {"type": "string", "description": "Report every thread of this process "
                                                         "(name or pid)"},
            "perfetto": {"type": "boolean", "description": "Also write Trace Event JSON the "
                                                          "user can open in ui.perfetto.dev"},
            "period_ms": {"type": "number", "description": "Period of the named thread(s); "
                                                           "default: found from the wake-ups"},
            "deadline_ms": {"type": "number", "description": "Deadline of the named thread(s); "
                                                             "default: the period"},
        }, ["path"]),
        lambda s, path, threads=None, process=None, perfetto=False, period_ms=None, deadline_ms=None:
            tools.trace_file(s, path, threads, process, perfetto_out="" if perfetto else None,
                             period_ms=period_ms, deadline_ms=deadline_ms),
        False,
    ),
    "qnx_audit": (
        "Show the audit log: every qbridge tool call (from the CLI or MCP) with its arguments, "
        "whether it can change the target, its outcome (ok / refused by a guard rail / error) "
        "and key results, plus experiment approvals. With verify=true (default) it checks the "
        "hash chain that makes later edits to the log detectable.",
        _schema({"limit": {"type": "integer", "description": "Most recent entries (default 30)"},
                 "verify": {"type": "boolean"}}),
        lambda s, limit=30, verify=True: tools.audit_log(s, limit, verify),
        False,
    ),
    "qnx_experiment_start": (
        "Start a measured fix experiment: build the project's current source (the baseline), "
        "deploy it, and measure the workload `repeats` times with a fixed length. Keeps the "
        "baseline binary and source for rollback. Needs write access.",
        _schema({
            "program": {"type": "string", "description": "Workload name on the target, e.g. rtdemo"},
            "project_dir": {"type": "string", "description": "Host folder with its Makefile"},
            "binary": {"type": "string", "description": "Built binary relative to project_dir"},
            "seconds": {"type": "integer"}, "repeats": {"type": "integer"},
            "args": {"type": "array", "items": {"type": "string"}},
        }, ["program", "project_dir"]),
        lambda s, program, project_dir, binary=None, seconds=10, repeats=3, args=None:
            experiments.start(s, program, project_dir, binary, seconds, repeats, args),
        True,
    ),
    "qnx_experiment_propose": (
        "Propose ONE change for an experiment, either an exact text replacement in one source "
        "file of the project (file, find, replace; 'find' must occur exactly once) or new "
        "workload arguments (args), for a scheduling change such as a thread priority with no "
        "source edit (-d and -o cannot be changed). Returns the diff. The change is NOT applied: "
        "a person must approve it by running 'qbridge experiment approve <id>' in a terminal. "
        "There is no tool to approve; ask the user.",
        _schema({
            "experiment_id": {"type": "string"}, "file": {"type": "string"},
            "find": {"type": "string"}, "replace": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "New workload arguments, e.g. ['-s', 'overload', '-a', '30']"},
            "description": {"type": "string", "description": "What the change does and why"},
            "diagnosis_id": {"type": "string", "description": "Diagnosis this change addresses"},
        }, ["experiment_id", "description"]),
        lambda s, experiment_id, description, file=None, find=None, replace=None, args=None,
               diagnosis_id=None:
            experiments.propose(s, experiment_id, file, find, replace, description, diagnosis_id,
                                args),
        False,
    ),
    "qnx_experiment_run": (
        "After a person approved the change: apply it, rebuild, redeploy under the same name, "
        "rerun the identical workload, and compare with the baseline (misses and worst-case "
        "response first). Says 'inconclusive' when the runs overlap. Needs write access.",
        _schema({"experiment_id": {"type": "string"}}, ["experiment_id"]),
        lambda s, experiment_id: experiments.run(s, experiment_id),
        True,
    ),
    "qnx_experiment_rollback": (
        "Undo an experiment: restore the source files and redeploy the saved baseline binary, "
        "then check the target's binary sha256 matches the baseline. Needs write access.",
        _schema({"experiment_id": {"type": "string"}}, ["experiment_id"]),
        lambda s, experiment_id: experiments.rollback(s, experiment_id),
        True,
    ),
    "qnx_experiment_keep": (
        "Keep a measured experiment's change applied.",
        _schema({"experiment_id": {"type": "string"}}, ["experiment_id"]),
        lambda s, experiment_id: experiments.keep(s, experiment_id),
        False,
    ),
    "qnx_experiment_show": (
        "Show an experiment (or list all when no id is given): state, baseline and candidate "
        "runs, the diff, who approved it and when, the comparison and its history.",
        _schema({"experiment_id": {"type": "string"}}),
        lambda s, experiment_id=None: (experiments.show(experiment_id) if experiment_id
                                       else {"experiments": experiments.list_all()}),
        False,
    ),
    "qnx_timing_check": (
        "Measure the target's timer lateness on one CPU (vmjitter -t: a priority-50 thread "
        "wakes every 1 ms with nothing else of ours running) and save it. This is the "
        "environment's noise floor; reports quote the latest check. Needs write access.",
        _schema({"cpu": {"type": "integer", "description": "Default 1"},
                 "seconds": {"type": "integer", "description": "Default 10"}}),
        lambda s, cpu=1, seconds=10: tools.timing_check(s, cpu, seconds),
        True,
    ),
    "qnx_report": (
        "Write a self-contained HTML report for an experiment, built only from saved data: "
        "verdict, before/after measurements and charts, where the time went, the diagnosis "
        "with its evidence, the change and who approved it, recommendation, limitations "
        "(including the saved timing check) and provenance. Returns the file path.",
        _schema({"experiment_id": {"type": "string"},
                 "diagnosis_id": {"type": "string",
                                  "description": "Default: the one linked to the proposal, "
                                                 "else the latest for the program"}},
                ["experiment_id"]),
        lambda s, experiment_id, diagnosis_id=None: tools.make_report(s, experiment_id, diagnosis_id),
        False,
    ),
    "qnx_runs": (
        "List saved measurement runs, newest first: label, misses, worst response.",
        _schema({"limit": {"type": "integer"}}),
        lambda s, limit=20: tools.list_runs(s, limit),
        False,
    ),
    "qnx_run_report": (
        "Show one saved measurement run: what ran (including the binary's sha256) and the "
        "full deadline report.",
        _schema({"run_id": {"type": "string"}}, ["run_id"]),
        lambda s, run_id: tools.get_run(s, run_id),
        False,
    ),
    "qnx_events": (
        "Wait for and return process deaths and core dumps on the target, pushed by qbridged "
        "(no polling). Returns recent history plus anything that happens during `wait` "
        "seconds. Use it right after qnx_run, or to watch a running system. Pass last_seq "
        "back as `since` to see only newer events.",
        _schema({
            "wait": {"type": "integer", "description": "Seconds to wait for events (1-120, default 10)"},
            "since": {"type": "integer", "description": "Only events with seq greater than this"},
            "include_noise": {"type": "boolean",
                              "description": "Also show deaths of shell helpers (sh, cat, ...)"},
        }),
        lambda s, wait=10, since=None, include_noise=False: tools.events(s, wait, since, include_noise),
        False,
    ),
    "qnx_crashes": (
        "List core files that QNX's dumper wrote on the target (newest first).",
        _schema({}),
        lambda s: tools.crashes(s),
        False,
    ),
    "qnx_crash": (
        "Analyse a crash: fetch a core file and the program that made it from the target, "
        "run gdb on the host, and return the signal, fault address, backtrace with arguments "
        "and locals, the source around the crash, and a one-line summary. Defaults to the "
        "newest core.",
        _schema({
            "core": {"type": "string", "description": "Core path on the target, or program name"},
            "binary": {"type": "string",
                       "description": "Local binary with debug symbols (default: fetched from target)"},
        }),
        lambda s, core=None, binary=None: tools.crash_report(s, core, binary),
        False,
    ),
}

_EXPECTED_ERRORS = (TargetError, PolicyError, QnxEnvError, FileNotFoundError, ValueError, TypeError,
                    experiments.ExperimentError)


class Server:
    def __init__(self, session: tools.Session, table: dict | None = None,
                 instructions: str | None = None):
        self.session = session
        self.tools = table if table is not None else TOOLS
        self.instructions = instructions or INSTRUCTIONS

    def tool_list(self) -> list[dict]:
        out = []
        for name, (desc, schema, _, writes) in self.tools.items():
            out.append({
                "name": name,
                "description": desc,
                "inputSchema": schema,
                "annotations": {"readOnlyHint": not writes, "destructiveHint": False,
                                "openWorldHint": False},
            })
        return out

    def call_tool(self, name: str, args: dict) -> dict:
        if name not in self.tools:
            raise KeyError(name)
        handler, writes = self.tools[name][2], self.tools[name][3]
        try:
            result = handler(self.session, **(args or {}))
        except _EXPECTED_ERRORS as e:
            audit.record("mcp", name, args, writes,
                         "refused" if isinstance(e, PolicyError) else "error",
                         error=f"{type(e).__name__}: {e}", target=self.session.target.host)
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                    "isError": True}
        except Exception as e:
            audit.record("mcp", name, args, writes, "error", error=f"{type(e).__name__}: {e}",
                         target=self.session.target.host)
            raise
        audit.record("mcp", name, args, writes, "ok", result=result,
                     target=self.session.target.host)
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "structuredContent": result, "isError": False}

    def handle(self, msg: dict) -> dict | None:
        """Handle one JSON-RPC message. Returns the response, or None for notifications."""
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if mid is None:
            return None  # notification, e.g. notifications/initialized
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "qbridge", "version": __version__},
                    "instructions": self.instructions,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tool_list()}
            elif method == "tools/call":
                try:
                    result = self.call_tool(params.get("name", ""), params.get("arguments") or {})
                except KeyError:
                    return _error(mid, -32602, f"unknown tool {params.get('name')!r}")
            else:
                return _error(mid, -32601, f"method not found: {method}")
        except Exception as e:  # never let one bad call kill the server
            traceback.print_exc(file=sys.stderr)
            return _error(mid, -32603, f"internal error: {e}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(allow_write: bool = False, stdin=None, stdout=None, replay: bool = False) -> None:
    if stdin is None:
        stdin = sys.stdin
        stdin.reconfigure(encoding="utf-8")
    if stdout is None:
        stdout = sys.stdout
        # Plain \n line endings and UTF-8, also on Windows.
        stdout.reconfigure(encoding="utf-8", newline="\n")
    if replay:
        from . import replaymode
        # No target: write tools are replayed, so nothing needs --allow-write.
        session = tools.Session(Target(host=None), Policy(allow_write=False))
        server = Server(session, replaymode.tool_table(replaymode.ReplayState()),
                        replaymode.INSTRUCTIONS)
        print(f"qbridge MCP server {__version__} in REPLAY mode: answers from data recorded on "
              "the target; nothing runs on a target", file=sys.stderr)
    else:
        session = tools.Session(Target.from_env(), Policy.from_env(allow_write))
        server = Server(session)
        print(f"qbridge MCP server {__version__} (write {'on' if session.policy.allow_write else 'off'})",
              file=sys.stderr)
    for line in stdin:
        # Windows PowerShell adds a UTF-8 BOM when it pipes text in.
        line = line.lstrip("﻿").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            reply = _error(None, -32700, f"parse error: {e}")
        else:
            if isinstance(msg, list):  # batch (older protocol versions)
                replies = [r for r in (server.handle(m) for m in msg) if r is not None]
                reply = replies or None
            else:
                reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
