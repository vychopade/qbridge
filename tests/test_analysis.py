"""Mutex priority-inversion rule, on /dev/qbridge/threads snapshots taken while
rtdemo ran on the target (faulty PTHREAD_PRIO_NONE build and fixed build)."""

import copy
import json

from qbridge import analysis, deadlines, runs, tools
from qbridge.policy import Policy
from qbridge.target import Result, Target


def _snaps(fixture_text, variant):
    return json.loads(fixture_text(f"diagnose_snapshots_{variant}.json"))["snapshots_kept"]


def _deadline_report(fixture_text):
    samples = deadlines.parse_samples_csv(fixture_text("rtdemo_samples.csv"))
    return deadlines.analyse(samples, 10000.0, 10000.0, "A-control")


def test_raw_snapshot_has_decoded_mutex_fields(fixture_text):
    threads = json.loads(fixture_text("mutex_snapshot_rtdemo.json"))["threads"]
    a = next(t for t in threads if t.get("thread_name") == "A-control")
    b = a["blocked_on"]
    assert a["state"] == "MUTEX"
    assert b["mutex_count"] == "0x90000000"          # NONRECURSIVE | PRIONONE
    assert b["mutex_protocol"] == "PTHREAD_PRIO_NONE" and b["mutex_shared"] is False
    owner = int(b["mutex_owner"], 16)
    assert owner & 0x80000000 and b["mutex_waiting"] is True       # A is waiting
    assert owner & 0xFFFF == b["mutex_owner_tid"] == 2               # C-logger
    assert (owner >> 16) & 0x7FFF == a["pid"] & 0x7FFF                # pid low bits
    assert b["mutex_owner_pid_bits_match"] is True


def test_faulty_build_gives_one_high_confidence_finding(fixture_text):
    (f,) = analysis.mutex_inversion(_snaps(fixture_text, "rtdemo"))
    assert f["rule"] == "mutex_priority_inversion" and f["confidence"] == "high"
    s = f["subject"]
    assert s["protocol"] == "PTHREAD_PRIO_NONE"
    assert (s["waiter"]["thread_name"], s["waiter"]["priority"]) == ("A-control", 30)
    assert (s["holder"]["thread_name"], s["holder"]["priority"]) == ("C-logger", 10)
    assert [(m["thread_name"], m["priority"]) for m in s["middle"]] == [("B-worker", 20)]
    assert f["observations"]["holder_below_waiter"] == f["observations"]["with_waiter"] == 9
    assert any("_NTO_SYNC_PRIONONE" in e for e in f["evidence"])
    assert any("not a documented interface" in u for u in f["could_not_determine"])
    assert "PTHREAD_PRIO_INHERIT" in f["suggested_fix"]


def test_deadline_report_is_added_as_evidence(fixture_text):
    rep = _deadline_report(fixture_text)
    (f,) = analysis.mutex_inversion(_snaps(fixture_text, "rtdemo"), rep)
    assert any("started with a long lock wait" in e for e in f["evidence"])
    assert not any("no deadline report" in u for u in f["could_not_determine"])
    (g,) = analysis.mutex_inversion(_snaps(fixture_text, "rtdemo"))
    assert any("no deadline report" in u for u in g["could_not_determine"])


def test_fixed_build_no_finding_and_inheritance_seen(fixture_text):
    d = analysis.diagnose(_snaps(fixture_text, "rtdemo_fixed"))
    assert d["findings"] == []
    assert "does not prove there is none" in d["summary"]
    (x,) = d["inheritance_observed"]
    assert x["protocol"] == "PTHREAD_PRIO_INHERIT"
    assert (x["holder"], x["holder_priority"], x["holder_real_priority"]) == ("C-logger", 30, 10)
    assert "inheritance was seen working" in d["summary"]


def test_unknown_holder_is_not_guessed(fixture_text):
    snaps = copy.deepcopy(_snaps(fixture_text, "rtdemo"))
    for snap in snaps:
        for t in snap:
            if t.get("state") == "MUTEX":
                t["blocked_on"]["mutex_owner_pid_bits_match"] = False
    assert analysis.mutex_inversion(snaps) == []


def test_weaker_evidence_lowers_confidence(fixture_text):
    snaps = copy.deepcopy(_snaps(fixture_text, "rtdemo"))
    for snap in snaps:
        for t in snap:
            if t.get("state") == "MUTEX":
                t["blocked_on"].pop("mutex_protocol")          # protocol unreadable
            if t.get("thread_name") == "B-worker":
                t["state"] = "NANOSLEEP"                        # no middle thread seen
    (f,) = analysis.mutex_inversion(snaps)
    assert f["confidence"] == "low"
    assert f["could_not_determine"][0].startswith("The mutex's protocol")
    assert "PTHREAD_PRIO_INHERIT" not in f["suggested_fix"]


def test_diagnose_without_waits_says_not_observed():
    d = analysis.diagnose([[{"pid": 1, "tid": 1, "name": "x", "state": "READY", "priority": 10}]])
    assert d["findings"] == [] and "not observed" in d["summary"]


class Snapper(Target):
    def __init__(self, snapshot_text):
        super().__init__(host="192.0.2.10")
        self.text = snapshot_text

    def run(self, argv, timeout=60):
        return Result("cat", 0, self.text, "")


def test_diagnose_tool_on_live_snapshot_with_saved_run(fixture_text, tmp_path, monkeypatch):
    monkeypatch.setenv("QBRIDGE_HOME", str(tmp_path))
    rep = _deadline_report(fixture_text)
    runs.save("20261117-000000-baseline", {"label": "baseline"}, None,
              fixture_text("rtdemo_samples.csv"), rep)
    t = Snapper(fixture_text("mutex_snapshot_rtdemo.json"))
    out = tools.diagnose(tools.Session(t, Policy()), snapshots=3,
                         run_id="20261117-000000-baseline")
    assert out["snapshots"] == 3
    assert out["findings"][0]["confidence"] == "high"
    assert out["deadline_evidence_from"] == "saved run 20261117-000000-baseline"
    saved = json.loads((tmp_path / "diagnoses" / f"{out['diagnosis_id']}.json").read_text("utf-8"))
    assert saved["result"]["findings"][0]["rule"] == "mutex_priority_inversion"
    assert len(saved["snapshots_kept"]) == 3
