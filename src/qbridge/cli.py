"""Command-line interface. Every command prints JSON so tools and people can share it."""

from __future__ import annotations

import argparse
import json
import sys

from . import experiments, tools
from .policy import Policy, PolicyError
from .qnxenv import QnxEnvError
from .target import Target, TargetError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qbridge", description="Inspect and drive a QNX 8.0 target.")
    p.add_argument("--allow-write", action="store_true",
                   help="allow commands that change the target (deploy, run)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="target address, OS version and CPU info")

    ps = sub.add_parser("ps", help="processes, threads and who is blocked on whom")
    ps.add_argument("--process", help="only processes whose name contains this, or this pid")

    lg = sub.add_parser("logs", help="slog2 log entries")
    lg.add_argument("--process", help="buffer set / process name (substring)")
    lg.add_argument("--pid", type=int)
    lg.add_argument("--since", type=float, help="epoch seconds")
    lg.add_argument("--severity", help="minimum severity, e.g. warning")
    lg.add_argument("--grep", help="message contains this text")
    lg.add_argument("--limit", type=int, default=200, help="newest N entries (default 200)")

    b = sub.add_parser("build", help="build a folder with make, or one source file with qcc")
    b.add_argument("path")
    b.add_argument("--make-target")
    b.add_argument("--output", help="output file when building one source file")

    d = sub.add_parser("deploy", help="copy a file to the target's deploy folder (needs --allow-write)")
    d.add_argument("local_path")
    d.add_argument("--name", help="name or path under the deploy folder")

    r = sub.add_parser("run", help="run a deployed program on the target (needs --allow-write)")
    r.add_argument("path", help="name or path under the deploy folder")
    r.add_argument("args", nargs=argparse.REMAINDER)
    r.add_argument("--timeout", type=int, default=30)
    r.add_argument("--background", action="store_true", help="start it and return at once")

    k = sub.add_parser("kill", help="signal a deployed program (needs --allow-write)")
    k.add_argument("--pid", type=int)
    k.add_argument("--name", help="program name")
    k.add_argument("--signal", default="TERM")

    me = sub.add_parser("measure", help="run a periodic workload and report deadline statistics "
                                        "(needs --allow-write)")
    me.add_argument("program")
    me.add_argument("--seconds", type=int, default=10)
    me.add_argument("--label")
    me.add_argument("--deadline-us", type=float)
    me.add_argument("args", nargs="*",
                    help="extra program arguments, after --  (e.g. measure rtdemo -- -c 1)")

    sub.add_parser("deadlines", help="live deadline statistics from qbridged")

    dg = sub.add_parser("diagnose", help="snapshot thread states and apply the diagnosis rules")
    dg.add_argument("program", nargs="?", help="workload to start (needs --allow-write)")
    dg.add_argument("--seconds", type=int, default=6)
    dg.add_argument("--snapshots", type=int, default=40)
    dg.add_argument("--run-id", help="use this saved run's deadline report")

    ru = sub.add_parser("runs", help="list saved measurement runs")
    ru.add_argument("--limit", type=int, default=20)
    rs = sub.add_parser("run-report", help="show one saved measurement run")
    rs.add_argument("run_id")

    ev = sub.add_parser("events", help="process deaths and core dumps pushed by qbridged")
    ev.add_argument("--wait", type=int, default=10, help="seconds to wait (default 10)")
    ev.add_argument("--since", type=int, help="only events after this seq")
    ev.add_argument("--all", action="store_true", help="include shell helper deaths")

    sub.add_parser("crashes", help="list core files on the target")
    c = sub.add_parser("crash", help="backtrace and locals from a core file (default: newest)")
    c.add_argument("--core", help="core path on the target, or program name")
    c.add_argument("--binary", help="local binary with symbols (default: fetched from target)")

    im = sub.add_parser("image", help="changes to the mkqnximage image folder on the host")
    im_sub = im.add_subparsers(dest="image_command", required=True)
    ii = im_sub.add_parser("install", help="add qbridged to the image so it starts at boot")
    ii.add_argument("--binary", help="qbridged binary (default: target/qbridged/build/qbridged)")

    tc = sub.add_parser("timing-check", help="measure and save the target's timer lateness "
                                             "(needs --allow-write)")
    tc.add_argument("--cpu", type=int, default=1)
    tc.add_argument("--seconds", type=int, default=10)

    rp = sub.add_parser("report", help="write the HTML report for an experiment")
    rp.add_argument("experiment_id")
    rp.add_argument("--diagnosis-id")
    rp.add_argument("--out", help="output file (default ~/.qbridge/reports/<id>.html)")

    ex = sub.add_parser("experiment", help="measured fix experiments (baseline, change, compare)")
    ex_sub = ex.add_subparsers(dest="exp_command", required=True)
    es = ex_sub.add_parser("start", help="build and measure the baseline (needs --allow-write)")
    es.add_argument("program")
    es.add_argument("--project", required=True, help="source folder with the Makefile")
    es.add_argument("--binary", help="built binary, relative to the project (default build/<program>)")
    es.add_argument("--seconds", type=int, default=10)
    es.add_argument("--repeats", type=int, default=3)
    es.add_argument("args", nargs="*", help="extra workload arguments, after --")
    ep = ex_sub.add_parser("propose", help="propose one exact text replacement")
    ep.add_argument("experiment_id")
    ep.add_argument("--file", required=True)
    ep.add_argument("--find", required=True)
    ep.add_argument("--replace", required=True)
    ep.add_argument("--description", required=True)
    ea = ex_sub.add_parser("approve", help="a person approves the proposed change (interactive)")
    ea.add_argument("experiment_id")
    for name, text in (("run", "apply the approved change, rebuild, rerun, compare"),
                       ("rollback", "restore the source and redeploy the baseline binary"),
                       ("keep", "keep the measured change"), ("show", "show an experiment")):
        x = ex_sub.add_parser(name, help=text)
        x.add_argument("experiment_id")
    ex_sub.add_parser("list", help="list experiments")

    sv = sub.add_parser("serve", help="run as an MCP server on stdio")
    # Also accepted after 'serve', which is how MCP client configs usually read.
    sv.add_argument("--allow-write", dest="serve_allow_write", action="store_true",
                    help="allow deploy and run")
    return p


def dispatch(s: tools.Session, a: argparse.Namespace) -> dict:
    match a.command:
        case "info":
            return tools.info(s)
        case "ps":
            return tools.ps(s, a.process)
        case "logs":
            return tools.logs(s, a.process, a.pid, a.since, a.severity, a.grep, a.limit)
        case "build":
            return tools.build(s, a.path, a.make_target, a.output)
        case "deploy":
            return tools.deploy(s, a.local_path, a.name)
        case "run":
            return tools.run(s, a.path, a.args, a.timeout, a.background)
        case "kill":
            return tools.kill(s, a.pid, a.name, a.signal)
        case "measure":
            return tools.measure(s, a.program, a.seconds, a.args, a.label, a.deadline_us)
        case "deadlines":
            return tools.deadline_stats(s)
        case "diagnose":
            return tools.diagnose(s, a.program, a.seconds, a.snapshots, a.run_id)
        case "runs":
            return tools.list_runs(s, a.limit)
        case "run-report":
            return tools.get_run(s, a.run_id)
        case "events":
            return tools.events(s, a.wait, a.since, a.all)
        case "crashes":
            return tools.crashes(s)
        case "crash":
            return tools.crash_report(s, a.core, a.binary)
        case "experiment":
            return experiment_command(s, a)
        case "timing-check":
            return tools.timing_check(s, a.cpu, a.seconds)
        case "report":
            return tools.make_report(s, a.experiment_id, a.diagnosis_id, a.out)
        case "image":
            from . import image
            return image.install_qbridged(s.target.image_dir, a.binary)
    raise ValueError(f"unknown command {a.command}")


def approve_interactively(exp_id: str, stdin=None, stdout=None) -> dict:
    """Show the proposed diff and ask a person to type 'yes'.

    This is the only way to approve a change. It refuses to run without an
    interactive terminal, so a script or an AI assistant driving qbridge
    cannot approve its own proposal.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stderr
    if not stdin.isatty():
        raise PolicyError("approval must be typed by a person in an interactive terminal")
    e = experiments.load(exp_id)
    p = e.get("proposal")
    if not p:
        raise experiments.ExperimentError("this experiment has no proposed change")
    print(f"Experiment {exp_id}: {e['program']} in {e['project_dir']}", file=stdout)
    print(f"Proposed change: {p['description']}\n", file=stdout)
    print(p["diff"], file=stdout)
    print("This will edit the file above, rebuild, redeploy and rerun the workload "
          f"({e['workload']['repeats']} x {e['workload']['seconds']} s).", file=stdout)
    print("Type yes to approve, anything else to decline: ", end="", file=stdout, flush=True)
    answer = stdin.readline()
    return experiments.approve(exp_id, answer, "qbridge CLI prompt")


def experiment_command(s: tools.Session, a: argparse.Namespace) -> dict:
    match a.exp_command:
        case "start":
            return experiments.start(s, a.program, a.project, a.binary, a.seconds, a.repeats, a.args)
        case "propose":
            return experiments.propose(s, a.experiment_id, a.file, a.find, a.replace, a.description)
        case "approve":
            return approve_interactively(a.experiment_id)
        case "run":
            return experiments.run(s, a.experiment_id)
        case "rollback":
            return experiments.rollback(s, a.experiment_id)
        case "keep":
            return experiments.keep(s, a.experiment_id)
        case "show":
            return experiments.show(a.experiment_id)
        case "list":
            return {"experiments": experiments.list_all()}
    raise ValueError(a.exp_command)


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.command == "serve":
        from .server import serve
        serve(allow_write=a.allow_write or a.serve_allow_write)
        return 0
    s = tools.Session(Target.from_env(), Policy.from_env(a.allow_write))
    try:
        out = dispatch(s, a)
    except (TargetError, PolicyError, QnxEnvError, FileNotFoundError, ValueError,
            experiments.ExperimentError) as e:
        print(json.dumps({"error": str(e), "type": type(e).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    if a.command == "build" and not out["ok"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
