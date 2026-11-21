"""The command allowlist at Target.run: what qbridge sends is allowed, anything else is refused
before ssh starts."""

import pytest

from qbridge import commands, tools
from qbridge.policy import Policy, PolicyError
from qbridge.target import Target

ROOT = "/data/qbridge"

ALLOWED = [
    ["pidin", "-F", "%a|%b|%n|%p|%J|%B|%h"],
    ["pidin", "info"],
    ["slog2info", "-f", "-S", "-t"],
    ["uname", "-a"],
    ["cat", "/dev/qbridge/threads"],
    ["cat", "/dev/qbridge/deadlines"],
    ["cat", "/data/qbridge/rtdemo.samples.csv"],
    ["tail", "-1", "/data/qbridge/rtdemo.log"],
    ["openssl", "dgst", "-sha256", "/data/qbridge/rtdemo"],
    ["mkdir", "-p", "/data/qbridge"],
    ["kill", "-s", "ABRT", "12", "13"],
    ["timeout", "30", "/data/qbridge/rtdemo", "-d", "10", "-o", "/data/qbridge/x.csv"],
    ["timeout", "10", "cat", "/dev/qbridge/events"],
    ["/data/qbridge/rtdemo", "-d", "5"],
]

REFUSED = [
    ["rm", "-rf", "/"],
    ["sh", "-c", "rm -rf /"],                         # raw script, not a named template
    ["cat", "/etc/shadow"],
    ["cat", "/data/qbridge/../../etc/passwd"],         # .. escape
    ["cat", "/data/qbridge-evil/x"],                   # look-alike prefix
    ["tail", "-1", "/etc/passwd"],
    ["openssl", "enc", "-d", "/data/qbridge/x"],
    ["mkdir", "-p", "/etc/cron.d"],
    ["kill", "-s", "STOP", "5"],
    ["kill", "-s", "KILL", "1; rm -rf /"],
    ["timeout", "5", "/bin/sh"],
    ["timeout", "5", "cat", "/etc/passwd"],
    ["slog2info", "-c"],                               # would clear the system log
    ["pidin", "-p", "1", "kill"],
    ["slay", "qbridged"],
    [],
]


@pytest.mark.parametrize("argv", ALLOWED, ids=lambda a: " ".join(a)[:40])
def test_commands_qbridge_uses_are_allowed(argv):
    commands.check(argv, ROOT)


@pytest.mark.parametrize("argv", REFUSED, ids=lambda a: " ".join(a)[:40] or "empty")
def test_other_commands_are_refused(argv):
    with pytest.raises(PolicyError):
        commands.check(argv, ROOT)


def test_named_scripts_render_validate_and_quote():
    s = commands.script("install", ROOT, tmp="/data/qbridge/a b.new", dest="/data/qbridge/a b")
    assert s[:2] == ["sh", "-c"] and s.name == "install"
    assert "'/data/qbridge/a b.new'" in s[2]
    commands.check(s, ROOT)
    bg = commands.script("start_background", ROOT,
                         argv=["timeout", "60", "/data/qbridge/rtdemo", "-d", "8"],
                         log="/data/qbridge/rtdemo.log")
    assert bg[2].endswith("< /dev/null > /data/qbridge/rtdemo.log 2>&1 & echo $!")
    for name in ("info", "list_cores", "list_exefiles"):
        commands.check(commands.script(name, ROOT), ROOT)
    commands.check(commands.script("log_tail", ROOT, log="/data/qbridge/x.log"), ROOT)


@pytest.mark.parametrize("name,params", [
    ("install", {"tmp": "/etc/passwd.new", "dest": "/etc/passwd"}),
    ("start_background", {"argv": ["/data/qbridge/x"], "log": "/data/qbridge/x.log"}),
    ("start_background", {"argv": ["timeout", "5", "/bin/sh"], "log": "/data/qbridge/x.log"}),
    ("start_background", {"argv": ["timeout", "5", "/data/qbridge/x"], "log": "/tmp/x"}),
    ("log_tail", {"log": "/var/log/secure"}),
    ("list_cores", {"dirs": ("/etc",)}),
    ("anything_else", {}),
])
def test_script_parameters_are_validated(name, params):
    with pytest.raises(PolicyError):
        commands.script(name, ROOT, **params)


def test_target_run_refuses_before_ssh(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ssh must not be started")
    monkeypatch.setattr("qbridge.target.subprocess.run", boom)
    t = Target(host="192.0.2.10")
    with pytest.raises(PolicyError, match="allowlist"):
        t.run(["rm", "-rf", "/data"])
    with pytest.raises(PolicyError, match="uploads may only go into"):
        t.put("local.bin", "/system/bin/qbridged")


def test_session_gives_target_the_policy_deploy_root():
    s = tools.Session(Target(host="192.0.2.10"), Policy(deploy_root="/data/other"))
    assert s.target.deploy_root == "/data/other"
    commands.check(["cat", "/data/other/x.csv"], s.target.deploy_root)
    with pytest.raises(PolicyError):
        commands.check(["cat", "/data/qbridge/x.csv"], s.target.deploy_root)
