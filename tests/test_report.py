"""HTML report built from the live experiment recorded on the target."""

import copy
import json
import re

from qbridge import deadlines, report, runs, tools
from qbridge.policy import Policy
from qbridge.target import Target

TIMING = {"mode": "timer", "cpu": 1, "priority": 50, "seconds": 10, "period_us": 1000,
          "wakeups": 9497, "max_late_us": 4925.9, "mean_late_us": 333.7, "late_over_1ms": 494,
          "measured": "2026-11-17T13:30:00", "check_id": "t1"}


def _exp(fixture_text):
    return json.loads(fixture_text("experiment_live/experiment.json"))


def _loader(fixture_text):
    """Run reports are the live ones; samples for the charts come from recorded
    rtdemo runs of the same two builds."""
    def load(run_id):
        rep = json.loads(fixture_text(f"experiment_live/{run_id}.report.json"))
        variant = "rtdemo" if "baseline" in run_id else "rtdemo_fixed"
        return {"report": rep, "samples": fixture_text(f"{variant}_samples.csv"),
                "summary": {"cpu": 1}}
    return load


def _diag(fixture_text):
    from qbridge import analysis
    snaps = json.loads(fixture_text("diagnose_snapshots_rtdemo.json"))["snapshots_kept"]
    return {"diagnosis_id": "d1", "taken": "2026-11-17T12:54:00",
            "result": analysis.diagnose(snaps)}


def test_report_contains_the_measured_numbers(fixture_text):
    e = _exp(fixture_text)
    html = report.build(e, _loader(fixture_text), _diag(fixture_text), [TIMING], "2026-11-17 13:30")
    text = re.sub(r"<[^>]+>", " ", html)
    assert "improved" in text
    assert "302–310" in text and "→" in text                   # misses before
    assert "37.0–37.1 ms" in text                               # worst before
    assert "4.1–5.4 ms" in text                                 # worst after
    assert "2.9–2.9" not in text                                # equal values collapse
    assert e["comparison"]["explanation"].split(".")[0] in text
    assert "20261117-130016-baseline-1" in text                 # short ids in the table
    # every full run id appears (provenance)
    for r in e["baseline"]["runs"] + e["candidate"]["runs"]:
        assert r["run_id"] in html
    assert e["baseline"]["binary_sha256"] in html


def test_report_sections(fixture_text):
    html = report.build(_exp(fixture_text), _loader(fixture_text), _diag(fixture_text), [TIMING])
    for heading in ("Measurements", "Response time", "Where the time went", "Diagnosis",
                    "The change", "Recommendation", "Limitations", "Provenance"):
        assert f"<h2>{heading}</h2>" in html
    assert "high confidence" in html
    assert '<span class="add">+#define LOCK_PROTOCOL PTHREAD_PRIO_INHERIT</span>' in html
    assert "Approved by a person via qbridge CLI prompt" in html
    assert "do not prove a worst-case response time" in html
    assert "9497 timer wake-ups" in html and "4.9 ms" in html      # timing check quoted
    assert "simulated, educational demo" in html
    assert "All threads ran on CPU 1" in html
    assert "6 of 6 runs" in html                                   # cross-checks
    assert html.count("<svg") == 3                                 # two series + distribution


def test_report_is_self_contained_and_escaped(fixture_text):
    e = _exp(fixture_text)
    e["proposal"]["description"] = "<script>alert(1)</script> & co"
    html = report.build(e, _loader(fixture_text))
    assert "<script" not in html.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in html
    assert not re.search(r'(src|href)="https?://', html)


def test_rolled_back_improvement_recommendation(fixture_text):
    html = report.build(_exp(fixture_text), _loader(fixture_text))
    assert "Keep the change" in html and "rolled back afterwards" in html


def test_inconclusive_recommendation(fixture_text):
    e = copy.deepcopy(_exp(fixture_text))
    e["comparison"]["verdict"] = "inconclusive"
    html = report.build(e, _loader(fixture_text))
    assert "Do not rely on this change yet" in html and "v-inconclusive" in html


def test_report_tool_writes_file(fixture_text, tmp_path, monkeypatch):
    monkeypatch.setenv("QBRIDGE_HOME", str(tmp_path))
    e = _exp(fixture_text)
    load = _loader(fixture_text)
    for r in e["baseline"]["runs"] + e["candidate"]["runs"]:
        d = load(r["run_id"])
        runs.save(r["run_id"], {"label": r["run_id"]}, None, d["samples"], d["report"])
    (tmp_path / "experiments" / e["id"]).mkdir(parents=True)
    (tmp_path / "experiments" / e["id"] / "experiment.json").write_text(json.dumps(e), "utf-8")
    runs.save_timing_check(TIMING)
    out = tools.make_report(tools.Session(Target(host="192.0.2.10"), Policy()), e["id"])
    html = open(out["report"], encoding="utf-8").read()
    assert out["report"].endswith(f"{e['id']}.html") and out["bytes"] > 20000
    assert "9497 timer wake-ups" in html and len(out["timing_checks_used"]) == 1
    assert out["diagnosis_used"] is None and "<h2>Diagnosis</h2>" not in html


def test_chart_handles_misses_and_downsampling():
    samples = [deadlines.Sample(release=i * 1000.0, response=(15000.0 if i % 7 == 0 else 1000.0))
               for i in range(5000)]
    svg = report._series_chart(samples, 10000.0, 20.0, "pts before", "t")
    assert svg.count("<circle") <= 2600
    assert '<g class="miss"><circle' in svg
