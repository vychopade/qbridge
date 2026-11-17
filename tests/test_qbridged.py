"""Tests against JSON recorded from qbridged running on a QNX 8.0 target."""

import json

from qbridge import tools
from qbridge.parsers import pidin, qbridged
from qbridge.policy import Policy
from qbridge.target import Result, Target


def _threads(fixture_text):
    return qbridged.parse_threads(fixture_text("qbridged_threads.json"))


def test_every_fixture_file_is_valid_json(fixture_text):
    for name in ("qbridged_threads.json", "qbridged_procs.json",
                 "qbridged_blocked.json", "qbridged_crashes.json"):
        json.loads(fixture_text(name))


def test_threads_match_pidin_shape(fixture_text):
    ts = _threads(fixture_text)
    assert len(ts) > 100
    idle = next(t for t in ts if (t.pid, t.tid) == (1, 1))
    assert idle.name == "procnto-smp-instr"
    assert idle.thread_name == "idle_cpu_0"
    assert idle.policy == "FIFO"


def test_full_thread_names_where_pidin_truncates(fixture_text):
    names = {t.thread_name for t in _threads(fixture_text)}
    assert "Entropy Collection Thread" in names


def test_reply_edges_carry_channel_and_duration(fixture_text):
    ts = _threads(fixture_text)
    ksh = [t for t in ts if t.name == "ksh" and t.state == "REPLY"]
    assert len(ksh) == 4
    for t in ksh:
        assert t.blocked_on_pid == 49157 and t.chid == 1
        assert t.blocked_ms is not None and t.blocked_ms > 0
    edges = pidin.blocked_graph(ts)
    e = next(e for e in edges if e["from_name"] == "ksh")
    assert e["to_name"] == "devc-con" and e["chid"] == 1 and "blocked_ms" in e


def test_receive_threads_have_chid_but_no_pid(fixture_text):
    for t in _threads(fixture_text):
        if t.state == "RECEIVE":
            assert t.blocked_on_pid is None and t.chid is not None


def test_blocked_file_has_no_snapshot_noise(fixture_text):
    edges = json.loads(fixture_text("qbridged_blocked.json"))["edges"]
    assert edges and all(e["from_name"] != "qbridged" for e in edges)
    assert all(e["state"] in ("SEND", "REPLY") for e in edges)


def test_crashes_file_lists_the_demo_core(fixture_text):
    c = json.loads(fixture_text("qbridged_crashes.json"))
    assert c["core_dir"] == "/data/var/dumper"
    assert any(x["path"].endswith("sensor.core") for x in c["cores"])


class Both(Target):
    def __init__(self, cat_out, cat_rc=0, pidin_out=""):
        super().__init__(host="192.0.2.10")
        self.cat_out, self.cat_rc, self.pidin_out = cat_out, cat_rc, pidin_out

    def run(self, argv, timeout=60):
        if argv[0] == "cat":
            return Result("cat", self.cat_rc, self.cat_out, "")
        return Result("pidin", 0, self.pidin_out, "")


def test_ps_prefers_qbridged(fixture_text):
    s = tools.Session(Both(fixture_text("qbridged_threads.json")), Policy())
    out = tools.ps(s, "ksh")
    assert out["source"] == "qbridged"
    assert out["blocked_on"][0]["blocked_ms"] > 0


def test_ps_falls_back_to_pidin(fixture_text):
    t = Both("", cat_rc=1, pidin_out=fixture_text("pidin_threads.txt"))
    out = tools.ps(tools.Session(t, Policy()), "ksh")
    assert out["source"] == "pidin"
    assert "blocked_ms" not in out["blocked_on"][0]
