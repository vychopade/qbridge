"""Command allowlist: every command qbridge sends to the target is checked here.

qbridge has no free-form command tool; each tool builds its own argv. This
module is a second line of defence at the single place commands leave the
host (Target.run): a command is sent only if its program is listed below
and its arguments pass that program's rule. Anything else is refused with
PolicyError before ssh is started.

Shell scripts (`sh -c`) are allowed only as named templates whose
parameters are validated and quoted here; an arbitrary `sh -c` string is
refused.
"""

from __future__ import annotations

import posixpath
import re
import shlex

from .policy import PolicyError

QBRIDGE_FILES = "/dev/qbridge"
CORE_DIRS = ("/data/var/dumper",)
SIGNALS = {"TERM", "INT", "HUP", "KILL", "QUIT", "ABRT", "USR1", "USR2"}


def _under(path: str, root: str) -> bool:
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    p = posixpath.normpath(path)
    root = posixpath.normpath(root)
    return p.startswith(root + "/")


def _deploy(path: str, root: str) -> None:
    if not _under(path, root):
        raise PolicyError(f"{path!r} is not inside the deploy folder {root}")


# -- named sh -c scripts ----------------------------------------------------------

class Script(list):
    """argv for `sh -c <script>` built from a named, validated template."""

    def __init__(self, name: str, text: str):
        super().__init__(["sh", "-c", text])
        self.name = name


def _q(v: str) -> str:
    return shlex.quote(v)


def script(name: str, root: str, **p) -> Script:
    """Render one of the known scripts after validating its parameters."""
    if name == "info":
        text = "uname -a; pidin info"
    elif name == "list_cores":
        dirs = p.get("dirs", CORE_DIRS)
        for d in dirs:
            if d not in CORE_DIRS:
                raise PolicyError(f"{d!r} is not a known core folder")
        globs = " ".join(f"{d}/*.core*" for d in dirs)
        text = f"stat -c '%n|%s|%Y' {globs} 2>/dev/null; true"
    elif name == "install":
        _deploy(p["tmp"], root)
        _deploy(p["dest"], root)
        text = f"chmod +x {_q(p['tmp'])} && mv -f {_q(p['tmp'])} {_q(p['dest'])}"
    elif name == "start_background":
        argv = p["argv"]
        if (len(argv) < 3 or argv[0] != "timeout" or not str(argv[1]).isdigit()):
            raise PolicyError("background programs must run under timeout <seconds>")
        _deploy(argv[2], root)
        _deploy(p["log"], root)
        text = f"{' '.join(_q(str(a)) for a in argv)} < /dev/null > {_q(p['log'])} 2>&1 & echo $!"
    elif name == "list_exefiles":
        text = ('for p in /proc/[0-9]*; do printf "%s|" "${p#/proc/}"; '
                'cat "$p/exefile" 2>/dev/null; echo; done')
    elif name == "log_tail":
        _deploy(p["log"], root)
        text = f"sleep 0.3; tail -c 4000 {_q(p['log'])} 2>/dev/null"
    else:
        raise PolicyError(f"unknown script {name!r}")
    return Script(name, text)


SCRIPT_NAMES = ("info", "list_cores", "install", "start_background", "list_exefiles", "log_tail")


# -- per-program argument rules -------------------------------------------------------

def _rule_any(args, root):
    return None


def _rule_cat(args, root):
    if len(args) != 1:
        return "cat takes exactly one file"
    a = args[0]
    if not (_under(a, QBRIDGE_FILES) or _under(a, root)):
        return f"cat may only read {QBRIDGE_FILES}/* or the deploy folder, not {a!r}"
    return None


def _rule_tail(args, root):
    if not args or not _under(args[-1], root):
        return "tail may only read files in the deploy folder"
    if any(not re.fullmatch(r"-[0-9]+|-c|[0-9]+", a) for a in args[:-1]):
        return "unexpected tail options"
    return None


def _rule_openssl(args, root):
    if len(args) != 3 or args[:2] != ["dgst", "-sha256"] or not _under(args[2], root):
        return "openssl is only used as 'dgst -sha256 <file in the deploy folder>'"
    return None


def _rule_mkdir(args, root):
    if len(args) != 2 or args[0] != "-p" or not (_under(args[1], root)
                                                   or posixpath.normpath(args[1]) == posixpath.normpath(root)):
        return "mkdir is only used as 'mkdir -p <deploy folder>'"
    return None


def _rule_kill(args, root):
    if len(args) < 3 or args[0] != "-s" or args[1] not in SIGNALS or not all(a.isdigit() for a in args[2:]):
        return "kill is only used as 'kill -s <allowed signal> <pids>'"
    return None


def _rule_timeout(args, root):
    if len(args) < 2 or not args[0].isdigit():
        return "timeout needs a number of seconds and a program"
    prog, rest = args[1], args[2:]
    if _under(prog, root):
        return None                       # a program qbridge deployed
    if prog == "cat" and len(rest) == 1 and _under(rest[0], QBRIDGE_FILES):
        return None                       # waiting on /dev/qbridge/events
    return f"timeout may only run deployed programs, not {prog!r}"


def _rule_pidin(args, root):
    # pidin only reports; but keep to the forms qbridge uses.
    if args in ([], ["info"]) or (len(args) == 2 and args[0] == "-F"):
        return None
    return "unexpected pidin arguments"


def _rule_slog2info(args, root):
    return None if all(a in ("-f", "-S", "-t") for a in args) else "unexpected slog2info arguments"


TRACE_MAX_SECONDS = 5


def _rule_tracelogger(args, root):
    """Only the capture qbridge uses: wide events, a short timed run with the
    kernel-call, interrupt and communication classes off, into the deploy folder."""
    shape = ["-w", "-n", "0", "-s", None, "-F1", "-F2", "-F6", "-f", None]
    if len(args) != len(shape) or any(s is not None and a != s for a, s in zip(args, shape)):
        return "tracelogger is only used as 'tracelogger -w -n 0 -s <secs> -F1 -F2 -F6 -f <file>'"
    try:
        secs = float(args[4])
    except ValueError:
        return "tracelogger -s needs a number of seconds"
    if not 0 < secs <= TRACE_MAX_SECONDS:
        return f"trace length must be 0-{TRACE_MAX_SECONDS} s"
    if not (_under(args[9], root) and args[9].endswith(".kev")):
        return "the trace file must be a .kev file in the deploy folder"
    return None


RULES = {
    "tracelogger": _rule_tracelogger,
    "pidin": _rule_pidin,
    "slog2info": _rule_slog2info,
    "uname": _rule_any,
    "cat": _rule_cat,
    "tail": _rule_tail,
    "openssl": _rule_openssl,
    "mkdir": _rule_mkdir,
    "kill": _rule_kill,
    "timeout": _rule_timeout,
}


def check(argv: list[str], root: str) -> None:
    """Raise PolicyError unless this command is allowed."""
    if not argv:
        raise PolicyError("empty command")
    prog = argv[0]
    if prog == "sh":
        if not isinstance(argv, Script) or argv.name not in SCRIPT_NAMES:
            raise PolicyError("shell scripts are only allowed as qbridge's named templates")
        return
    if _under(prog, root):
        return                            # running a program qbridge deployed
    rule = RULES.get(prog)
    if rule is None:
        raise PolicyError(f"{prog!r} is not on qbridge's command allowlist")
    problem = rule([str(a) for a in argv[1:]], root)
    if problem:
        raise PolicyError(f"refused: {problem}")
