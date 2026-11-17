"""Command-line interface. Every command prints JSON so tools and people can share it."""

from __future__ import annotations

import argparse
import json
import sys

from .parsers import pidin, slog2
from .target import Target, TargetError


def cmd_ip(t: Target, args: argparse.Namespace) -> dict:
    return {"host": t.resolve_host()}


def cmd_ps(t: Target, args: argparse.Namespace) -> dict:
    r = t.run(pidin.PIDIN_ARGS)
    if not r.ok:
        raise TargetError(f"pidin failed: {r.stderr.strip()}")
    threads = pidin.parse_threads(r.stdout)
    # Build the graph from every thread so edges keep the server's name.
    edges = pidin.blocked_graph(threads)
    cycles = pidin.find_cycles(edges)
    if args.process:
        want = args.process.lower()
        threads = [x for x in threads if want in x.name.lower() or str(x.pid) == args.process]
        pids = {x.pid for x in threads}
        edges = [e for e in edges if e["from_pid"] in pids or e["to_pid"] in pids]
        cycles = [c for c in cycles if pids & set(c)]
    return {
        "processes": pidin.processes(threads),
        "blocked_on": edges,
        "cycles": cycles,
    }


def cmd_logs(t: Target, args: argparse.Namespace) -> dict:
    r = t.run(slog2.SLOG2_ARGS)
    if not r.ok:
        raise TargetError(f"slog2info failed: {r.stderr.strip()}")
    entries = slog2.filter_entries(
        slog2.parse(r.stdout),
        process=args.process,
        pid=args.pid,
        since=args.since,
        min_severity=args.severity,
        contains=args.grep,
        limit=args.limit,
    )
    return {"count": len(entries), "entries": [e.to_dict() for e in entries]}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qbridge", description="Inspect a QNX 8.0 target.")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ip", help="show the target address").set_defaults(func=cmd_ip)

    ps = sub.add_parser("ps", help="processes, threads and who is blocked on whom")
    ps.add_argument("--process", help="only processes whose name contains this, or this pid")
    ps.set_defaults(func=cmd_ps)

    logs = sub.add_parser("logs", help="slog2 log entries")
    logs.add_argument("--process", help="buffer set / process name (substring)")
    logs.add_argument("--pid", type=int)
    logs.add_argument("--since", type=float, help="epoch seconds")
    logs.add_argument("--severity", help="minimum severity, e.g. warning")
    logs.add_argument("--grep", help="message contains this text")
    logs.add_argument("--limit", type=int, default=200, help="newest N entries (default 200)")
    logs.set_defaults(func=cmd_logs)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out = args.func(Target.from_env(), args)
    except (TargetError, ValueError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
