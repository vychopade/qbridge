"""qbridge gate: pass/fail on recorded runs from the reference target."""

import json
from pathlib import Path

from qbridge import deadlines

FX = Path(__file__).parent / "fixtures"


def _runs(name, side):
    e = json.loads((FX / name / "experiment.json").read_text(encoding="utf-8"))
    return [json.loads((FX / name / f"{r['run_id']}.report.json").read_text(encoding="utf-8"))
            for r in e[side]["runs"]]


def test_fault_fails_and_fix_passes_with_zero_misses():
    assert not deadlines.gate(_runs("experiment_live", "baseline"))["passed"]
    g = deadlines.gate(_runs("experiment_live", "candidate"))
    assert g["passed"] and g["misses"] == [0, 0, 0]
    assert "does not prove" in g["summary"]


def test_one_bad_run_fails_the_gate():
    g = deadlines.gate(_runs("experiment_live_overload", "candidate"))   # misses 0, 1, 0
    assert not g["passed"]
    assert g["failures"] == ["run 2: 1 misses (limit 0)"]
    assert deadlines.gate(_runs("experiment_live_overload", "candidate"), max_misses=1)["passed"]


def test_worst_response_limit_and_baseline_comparison():
    after = _runs("experiment_live_msgpass", "candidate")
    before = _runs("experiment_live_msgpass", "baseline")
    assert deadlines.gate(after, max_misses=None, baseline=before)["passed"]
    g = deadlines.gate(before, max_misses=None, baseline=after)
    assert not g["passed"] and any("baseline" in f for f in g["failures"])
    g = deadlines.gate(after, max_misses=None, max_worst_us=20000)
    assert not g["passed"] and g["failures"] == ["run 3: worst response 28.0 ms (limit 20.0 ms)"]
