"""`qbridge replay`: the offline quick check must reproduce every recorded result."""

from qbridge import cli, replay


def test_replay_reproduces_every_recorded_result():
    out = replay.run()
    kinds = [c["kind"] for c in out["checks"]]
    assert kinds.count("diagnosis") == 7 and kinds.count("trace") == 4 and kinds.count("experiment") == 5
    assert out["all_match"], [c["name"] for c in out["checks"] if not c["matches_target"]]
    # The fix that should not help is measured as not helping, not called "improved".
    by_name = {c["name"]: c for c in out["checks"]}
    assert by_name["experiment_live_wrongfix"]["result"] == ["verdict: inconclusive"]
    # Rules added after the recordings are reported apart, not compared with the target.
    fixed = by_name["scenario_overload_fixed"]
    assert fixed["result"] == ["no finding"] and fixed["matches_target"]
    assert fixed["newer_rules"][0].startswith("Late wake-up: A-control")
    assert by_name["scenario_condvar_fault"]["result"][0].startswith("Priority inversion on a condition variable")
    assert by_name["condvar_fault"]["result"] == ["Priority inversion on a condition variable: confirmed by the kernel trace"]
    assert all(not c.get("newer_rules") for n, c in by_name.items()
               if n in ("scenario_inversion", "scenario_overload_fault", "scenario_msgpass_fault"))
    by = {c["name"]: c for c in out["checks"]}
    assert "CPU preemption" in by["scenario_overload_fault"]["result"][0]
    assert "Priority inversion" in by["scenario_inversion"]["result"][0]
    assert by["scenario_overload_fixed"]["result"] == ["no finding"]
    assert "message passing" in by["scenario_msgpass_fault"]["result"][0]
    assert by["scenario_msgpass_fixed"]["result"] == ["no finding"]
    assert all("confirmed" in r for n in ("inversion", "overload_fault") for r in by[n]["result"])
    assert by["experiment_live_overload"]["result"] == ["verdict: improved"]
    assert by["experiment_live_condvar"]["result"] == ["verdict: improved"]


def test_replay_text_and_cli(capsys):
    text = replay.text(replay.run())
    assert "16 of 16 results are the same as qbridge reported on the target." in text
    assert "nothing new is measured" in text
    assert cli.main(["replay"]) == 0
    assert "16 of 16" in capsys.readouterr().out


def test_replay_reports_a_difference(monkeypatch):
    real = replay.analysis.diagnose

    def wrong(*a, **k):
        r = real(*a, **k)
        for f in r["findings"]:
            f["confidence"] = "low"
        return r
    monkeypatch.setattr(replay.analysis, "diagnose", wrong)
    out = replay.run()
    assert not out["all_match"]
    assert "DIFFERS from the target" in replay.text(out)
