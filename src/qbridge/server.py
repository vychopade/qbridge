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

from . import __version__, tools
from .policy import Policy, PolicyError
from .qnxenv import QnxEnvError
from .target import Target, TargetError

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "qbridge connects you to a QNX 8.0 target. Use qnx_ps and qnx_logs to see what is "
    "running and why it is stuck (blocked_on shows which process each thread waits on; "
    "cycles are likely deadlocks). When a program dies from a signal, use qnx_crash to get "
    "the backtrace from its core file. Use qnx_build to compile on the host. qnx_deploy and "
    "qnx_run change the target and only work if the user started qbridge with --allow-write."
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
        "programs in the deploy folder can be run. Needs write access.",
        _schema({
            "path": {"type": "string", "description": "Name or path under the deploy folder"},
            "args": {"type": "array", "items": {"type": "string"}},
            "timeout": {"type": "integer", "description": "Seconds before it is stopped (default 30)"},
        }, ["path"]),
        lambda s, path, args=None, timeout=30: tools.run(s, path, args, timeout),
        True,
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

_EXPECTED_ERRORS = (TargetError, PolicyError, QnxEnvError, FileNotFoundError, ValueError, TypeError)


class Server:
    def __init__(self, session: tools.Session):
        self.session = session

    def tool_list(self) -> list[dict]:
        out = []
        for name, (desc, schema, _, writes) in TOOLS.items():
            out.append({
                "name": name,
                "description": desc,
                "inputSchema": schema,
                "annotations": {"readOnlyHint": not writes, "destructiveHint": False,
                                "openWorldHint": False},
            })
        return out

    def call_tool(self, name: str, args: dict) -> dict:
        if name not in TOOLS:
            raise KeyError(name)
        handler = TOOLS[name][2]
        try:
            result = handler(self.session, **(args or {}))
        except _EXPECTED_ERRORS as e:
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                    "isError": True}
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
                    "instructions": INSTRUCTIONS,
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


def serve(allow_write: bool = False, stdin=None, stdout=None) -> None:
    if stdin is None:
        stdin = sys.stdin
        stdin.reconfigure(encoding="utf-8")
    if stdout is None:
        stdout = sys.stdout
        # Plain \n line endings and UTF-8, also on Windows.
        stdout.reconfigure(encoding="utf-8", newline="\n")
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
