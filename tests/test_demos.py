"""Deadlock and priority-inversion detection, on state recorded while the demo
apps (examples/deadlock, examples/inversion) were running on a QNX 8.0 target."""

import pytest

from qbridge import crash, tools
from qbridge.parsers import pidin, qbridged
from qbridge.policy import Policy, PolicyError
from qbridge.target import Result, Target


def _q(fixture_text, name):
    return qbridged.parse_threads(fixture_text(name))


# -- deadlock ------------------------------------------------------------------

@pytest.mark.parametrize("source", ["qbridged", "pidin"])
def test_deadlock_cycle_found(fixture_text, source):
    if source == "qbridged":
        threads = _q(fixture_text, "deadlock_threads.json")
    else:
        threads = pidin.parse_threads(fixture_text("deadlock_pidin.txt"))
    cycles = pidin.find_cycles(pidin.blocked_graph(threads))
    demo = [c for c in cycles if all(t.name == "deadlock" for t in threads if t.pid in c)]
    assert len(demo) == 1 and len(demo[0]) == 2


def test_deadlock_report_names_both_sides(fixture_text):
    threads = _q(fixture_text, "deadlock_threads.json")
    cycles = pidin.find_cycles(pidin.blocked_graph(threads))
    (d,) = pidin.describe_deadlocks(cycles, threads)
    states = {t["thread_name"]: t["state"] for t in d["threads"]}
    assert states == {"config-main": "REPLY", "sensor-main": "SEND"}
    assert all(t["blocked_ms"] > 1000 for t in d["threads"])
    assert d["explanation"].startswith("Deadlock:")


def test_no_inversion_reported_for_deadlock(fixture_text):
    assert pidin.find_inversions(_q(fixture_text, "deadlock_threads.json")) == []


# -- priority inversion ----------------------------------------------------------

@pytest.mark.parametrize("source", ["qbridged", "pidin"])
def test_inversion_found(fixture_text, source):
    if source == "qbridged":
        threads = _q(fixture_text, "inversion_threads.json")
    else:
        threads = pidin.parse_threads(fixture_text("inversion_pidin.txt"))
    (inv,) = pidin.find_inversions(threads)
    assert inv["active"]
    assert (inv["client"]["thread_name"], inv["client"]["priority"]) == ("client-high", 30)
    assert (inv["server"]["thread_name"], inv["server"]["priority"]) == ("server-low", 10)
    assert [(p["thread_name"], p["priority"]) for p in inv["preempted_by"]] == [("hog-medium", 20)]
    assert "_NTO_CHF_FIXED_PRIORITY" in inv["explanation"]


def test_inversion_has_blocked_time_from_qbridged(fixture_text):
    (inv,) = pidin.find_inversions(_q(fixture_text, "inversion_threads.json"))
    assert inv["blocked_ms"] > 1000


def test_inherited_priority_is_not_an_inversion():
    text = ("pid|tid|name|prio|STATE|Blocked|thread name\n"
            "1|1|/c| 30f|REPLY|2|client\n"
            "2|1|/s| 30f|READY||server\n"          # inherited the client's 30
            "3|1|/h| 20f|RUNNING||hog\n")
    assert pidin.find_inversions(pidin.parse_threads(text)) == []


def test_receive_blocked_server_threads_are_ignored():
    text = ("pid|tid|name|prio|STATE|Blocked|thread name\n"
            "1|1|/c| 30f|SEND|2|client\n"
            "2|1|/s| 10f|RECEIVE|1|idle\n")         # nobody working: not an inversion
    assert pidin.find_inversions(pidin.parse_threads(text)) == []


# -- crash summary for a stuck kernel call --------------------------------------------

def test_summary_points_at_user_frame_not_libc_stub():
    c = {"program": "/data/qbridge/deadlock", "signal": "SIGABRT", "frames": [
        {"function": "MsgSend", "file": "/builds/workspace/x/MsgSend.S", "line": 15,
         "args": {}, "locals": {}},
        {"function": "run_sensor", "file": "deadlock.c", "line": 64, "args": {}, "locals": {}},
    ]}
    s = crash.summarise(c)
    assert "in MsgSend(), called from run_sensor() at deadlock.c:64" in s


# -- background run and guarded kill ------------------------------------------------

class Fake(Target):
    def __init__(self, procs=""):
        super().__init__(host="192.0.2.10")
        self.procs, self.calls = procs, []

    def run(self, argv, timeout=60):
        self.calls.append(list(argv))
        if argv[:2] == ["sh", "-c"] and "& echo $!" in argv[2]:
            return Result("", 0, "4242\n", "")
        if argv[:2] == ["sh", "-c"] and "/proc/[0-9]*" in argv[2]:
            return Result("", 0, self.procs, "")
        if argv[:2] == ["sh", "-c"] and "tail -c" in argv[2]:
            return Result("", 0, "client: done\n", "")
        return Result(" ".join(argv), 0, "", "")


PROCS = ("1|/proc/boot/procnto-smp-instr\x00\n"
         "49157|/proc/boot/devc-con\x00 \n"
         "500|/data/qbridge/deadlock\x00\n"
         "501|/data/qbridge/deadlock\x00\n")


def test_background_run_detaches_and_logs():
    t = Fake()
    out = tools.run(tools.Session(t, Policy(allow_write=True)), "deadlock", timeout=99999,
                    background=True)
    cmd = t.calls[0][2]
    assert "< /dev/null" in cmd and "> /data/qbridge/deadlock.log 2>&1 &" in cmd
    assert cmd.startswith(f"timeout {tools.MAX_BACKGROUND_S} /data/qbridge/deadlock")
    assert out["timeout_pid"] == 4242 and out["log"] == "/data/qbridge/deadlock.log"


def test_kill_by_name_signals_every_matching_deployed_process():
    t = Fake(PROCS)
    out = tools.kill(tools.Session(t, Policy(allow_write=True)), name="deadlock", signal="abrt")
    assert ["kill", "-s", "ABRT", "500", "501"] in t.calls
    assert out["signal"] == "SIGABRT" and "qnx_crash" in out["hint"]
    assert out["logs"] == {"/data/qbridge/deadlock.log": "client: done\n"}


def test_kill_refuses_system_processes():
    t = Fake(PROCS)
    with pytest.raises(PolicyError, match="outside the deploy folder"):
        tools.kill(tools.Session(t, Policy(allow_write=True)), pid=49157)
    assert not any(c[0] == "kill" for c in t.calls)


def test_kill_needs_write_and_allowed_signal():
    with pytest.raises(PolicyError, match="read-only"):
        tools.kill(tools.Session(Fake(PROCS), Policy()), name="deadlock")
    with pytest.raises(ValueError, match="not allowed"):
        tools.kill(tools.Session(Fake(PROCS), Policy(allow_write=True)), name="deadlock",
                   signal="STOP")
