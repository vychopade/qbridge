"""qbridged's deadline statistics, recorded on the target while rtdemo reported
to it by message passing (qbridge_task.h). Each fixture holds rtdemo's own
summary line and /dev/qbridge/deadlines read right after the run, plus the
per-cycle samples from both sides."""

import copy
import csv
import io
import json

import pytest

from qbridge import deadlines, tools
from qbridge.parsers import qbridged
from qbridge.policy import Policy
from qbridge.target import Result, Target

VARIANTS = ["rtdemo", "rtdemo_fixed"]


def _load(fixture_text, variant):
    d = json.loads(fixture_text(f"qbridged_deadlines_{variant}.json"))
    summary = d["rtdemo_summary"]
    dl = qbridged.parse_deadlines(json.dumps(d["deadlines"]))
    task = next(t for t in dl["tasks"] if t["slot"] == summary["qbridged"]["slot"])
    return summary, dl, task


@pytest.mark.parametrize("variant", VARIANTS)
def test_qbridged_matches_rtdemo_summary(fixture_text, variant):
    summary, dl, task = _load(fixture_text, variant)
    assert summary["qbridged"] == {"status": "registered", "slot": task["slot"], "refused_pulses": 0}
    assert dl["rejected_pulses"] == 0
    assert (task["cycles"], task["misses"]) == (summary["cycles"], summary["misses"])
    assert task["name"] == "A-control" and task["period_us"] == task["deadline_us"] == 10000
    for f in qbridged.DEADLINE_FIELDS:
        assert task[f]["max_us"] == pytest.approx(summary[f]["max_us"], abs=1)
        assert task[f]["p99_us"] == pytest.approx(summary[f]["p99_us"], abs=1)
        assert task[f]["mean_us"] == pytest.approx(summary[f]["mean_us"], abs=1)


@pytest.mark.parametrize("variant", VARIANTS)
def test_qbridged_samples_match_rtdemo_samples(fixture_text, variant):
    mine = list(csv.DictReader(io.StringIO(fixture_text(f"qbridged_rtdemo_csv_{variant}.csv"))))
    theirs = list(csv.DictReader(io.StringIO(fixture_text(f"qbridged_samples_{variant}.csv"))))
    assert len(mine) == len(theirs) == 999
    for a, b in zip(mine, theirs):
        for f in ("response", "backlog", "latency", "lock_wait", "exec"):
            assert float(b[f"{f}_us"]) == pytest.approx(float(a[f"{f}_us"]), abs=1)


@pytest.mark.parametrize("variant", VARIANTS)
def test_host_report_cross_checks_with_qbridged(fixture_text, variant):
    _, _, task = _load(fixture_text, variant)
    samples = deadlines.parse_samples_csv(fixture_text(f"qbridged_rtdemo_csv_{variant}.csv"))
    report = deadlines.analyse(samples, 10000.0, 10000.0, "A-control")
    check = qbridged.cross_check(report, task)
    assert check["agrees"] is True and check["problems"] == []


def test_cross_check_reports_a_disagreement(fixture_text):
    _, _, task = _load(fixture_text, "rtdemo")
    samples = deadlines.parse_samples_csv(fixture_text("qbridged_rtdemo_csv_rtdemo.csv"))
    report = deadlines.analyse(samples, 10000.0)
    bad = copy.deepcopy(task)
    bad["misses"] += 1
    bad["lock_wait"]["max_us"] += 50
    check = qbridged.cross_check(report, bad)
    assert check["agrees"] is False and len(check["problems"]) == 2


def test_summary_text(fixture_text):
    _, _, task = _load(fixture_text, "rtdemo")
    text = qbridged.summarise_task(task)
    assert text.startswith("A-control (pid ") and "finished" in text and "worst lock wait" in text


class ReplayQ(Target):
    """Replays a measure run where the workload reported to qbridged."""

    def __init__(self, fixture_text, variant):
        super().__init__(host="192.0.2.10")
        d = json.loads(fixture_text(f"qbridged_deadlines_{variant}.json"))
        self.summary = json.dumps(d["rtdemo_summary"])
        self.deadlines = json.dumps(d["deadlines"])
        self.csv = fixture_text(f"qbridged_rtdemo_csv_{variant}.csv")

    def run(self, argv, timeout=60):
        if argv[0] == "timeout":
            return Result("", 0, self.summary + "\n", "")
        if argv == ["cat", qbridged.DEADLINES_PATH]:
            return Result("", 0, self.deadlines, "")
        if argv[0] == "cat":
            return Result("", 0, self.csv, "")
        return Result("", 0, "", "")


def test_measure_adds_qbridged_check(fixture_text, tmp_path, monkeypatch):
    monkeypatch.setenv("QBRIDGE_HOME", str(tmp_path))
    out = tools.measure(tools.Session(ReplayQ(fixture_text, "rtdemo"), Policy(allow_write=True)),
                        "rtdemo")
    assert out["cross_check"]["agrees"] is True
    assert out["qbridged_check"]["checked"] is True and out["qbridged_check"]["agrees"] is True


def test_deadline_stats_tool(fixture_text):
    t = ReplayQ(fixture_text, "rtdemo_fixed")
    out = tools.deadline_stats(tools.Session(t, Policy()))
    assert all("summary" in x for x in out["tasks"])
    assert any(x["misses"] == 0 for x in out["tasks"])
