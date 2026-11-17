from pathlib import Path

from qbridge.target import SSH_MAC, Target


def test_ssh_args_force_mac_and_batch_mode():
    t = Target(host="192.0.2.10", key=Path("k"))
    args = t.ssh_args("pidin")
    assert args[0] == "ssh"
    assert args[args.index("-m") + 1] == SSH_MAC == "hmac-sha2-256"
    assert "BatchMode=yes" in args
    assert args[-2:] == ["root@192.0.2.10", "pidin"]


def test_from_env(monkeypatch):
    monkeypatch.setenv("QBRIDGE_HOST", "192.0.2.20")
    monkeypatch.setenv("QBRIDGE_USER", "qnxuser")
    monkeypatch.setenv("QBRIDGE_PORT", "2222")
    t = Target.from_env()
    assert (t.host, t.user, t.port) == ("192.0.2.20", "qnxuser", 2222)


def test_run_quotes_arguments(monkeypatch):
    seen = {}

    class P:
        returncode, stdout, stderr = 0, "ok", ""

    def fake_run(args, **kw):
        seen["args"] = args
        return P()

    monkeypatch.setattr("qbridge.target.subprocess.run", fake_run)
    r = Target(host="192.0.2.10").run(["pidin", "-F", "%a|%b"])
    assert r.ok and r.stdout == "ok"
    assert seen["args"][-1] == "pidin -F '%a|%b'"
