"""Events pushed by qbridged, using a stream recorded on a QNX 8.0 target."""

from qbridge import tools
from qbridge.parsers import qbridged
from qbridge.policy import Policy
from qbridge.target import Result, Target


def _events(fixture_text):
    return qbridged.parse_events(fixture_text("qbridged_events.jsonl"))


def test_parse_recorded_stream(fixture_text):
    evs = _events(fixture_text)
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    core = next(e for e in evs if e["type"] == "core")
    assert core["name"] == "sensor" and core["core"] == "/data/var/dumper/sensor.core"
    # The crash shows up as a core event, then the death of the same pid.
    death = next(e for e in evs if e["type"] == "death" and e["pid"] == core["pid"])
    assert death["seq"] == core["seq"] + 1 and death["name"] == "sensor"


def test_noise_hidden_by_default(fixture_text):
    kept, hidden = qbridged.filter_events(_events(fixture_text))
    assert {e["name"] for e in kept} == {"sensor"}
    assert hidden > 0
    everything, none_hidden = qbridged.filter_events(_events(fixture_text), include_noise=True)
    assert none_hidden == 0 and len(everything) == len(_events(fixture_text))


def test_since_skips_seen_events(fixture_text):
    kept, _ = qbridged.filter_events(_events(fixture_text), since=7, include_noise=True)
    assert all(e["seq"] > 7 for e in kept)


def test_cut_off_last_line_is_ignored():
    text = '{"seq": 1, "type": "death", "pid": 5, "name": "x"}\n{"seq": 2, "ty'
    assert [e["seq"] for e in qbridged.parse_events(text)] == [1]


class EventsTarget(Target):
    def __init__(self, out, rc=124):
        super().__init__(host="192.0.2.10")
        self.out, self.rc, self.calls = out, rc, []

    def run(self, argv, timeout=60):
        self.calls.append((list(argv), timeout))
        return Result(" ".join(argv), self.rc, self.out, "")


def test_events_tool_waits_and_reports_last_seq(fixture_text):
    t = EventsTarget(fixture_text("qbridged_events.jsonl"))
    out = tools.events(tools.Session(t, Policy()), wait=5)
    argv, ssh_timeout = t.calls[0]
    assert argv == ["timeout", "5", "cat", "/dev/qbridge/events"] and ssh_timeout > 5
    assert [e["name"] for e in out["events"]] == ["sensor", "sensor"]
    recorded = _events(fixture_text)
    assert out["last_seq"] == max(e["seq"] for e in recorded)
    assert out["hidden_noise"] == len(recorded) - 2


def test_events_tool_with_nothing_new():
    out = tools.events(tools.Session(EventsTarget(""), Policy()), wait=1, since=9)
    assert out["events"] == [] and out["last_seq"] == 9


def test_events_tool_reports_missing_daemon():
    import pytest
    from qbridge.target import TargetError
    with pytest.raises(TargetError, match="qbridged running"):
        tools.events(tools.Session(EventsTarget("", rc=1), Policy()), wait=1)


def test_deploy_copies_beside_and_renames(tmp_path):
    f = tmp_path / "app"
    f.write_bytes(b"x")

    class T(Target):
        def __init__(self):
            super().__init__(host="192.0.2.10")
            self.runs, self.puts = [], []

        def run(self, argv, timeout=60):
            self.runs.append(argv)
            return Result(" ".join(argv), 0, "", "")

        def put(self, local, remote, timeout=120):
            self.puts.append(remote)

    t = T()
    tools.deploy(tools.Session(t, Policy(allow_write=True)), str(f))
    assert t.puts == ["/data/qbridge/app.new"]
    assert "mv -f /data/qbridge/app.new /data/qbridge/app" in t.runs[-1][-1]
