import pytest

from qbridge.policy import Policy, PolicyError


def test_read_only_by_default(monkeypatch):
    monkeypatch.delenv("QBRIDGE_ALLOW_WRITE", raising=False)
    p = Policy.from_env()
    assert not p.allow_write
    with pytest.raises(PolicyError, match="--allow-write"):
        p.require_write("deploy")


def test_flag_or_env_enables_write(monkeypatch):
    monkeypatch.delenv("QBRIDGE_ALLOW_WRITE", raising=False)
    assert Policy.from_env(allow_write=True).allow_write
    monkeypatch.setenv("QBRIDGE_ALLOW_WRITE", "1")
    assert Policy.from_env().allow_write


@pytest.mark.parametrize("given,expected", [
    ("hello", "/data/qbridge/hello"),
    ("apps/hello", "/data/qbridge/apps/hello"),
    ("/data/qbridge/x", "/data/qbridge/x"),
])
def test_deploy_path_inside_root(given, expected):
    assert Policy().deploy_path(given) == expected


@pytest.mark.parametrize("bad", ["../etc/passwd", "/proc/boot/procnto", "/data/qbridge-evil/x", "/data"])
def test_deploy_path_escapes_rejected(bad):
    with pytest.raises(PolicyError):
        Policy().deploy_path(bad)
