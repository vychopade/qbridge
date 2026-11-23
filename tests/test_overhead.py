"""qbridge's own overhead, measured on the target on Nov 23 (tests/fixtures/overhead/).

The README states the result in numbers; these tests recompute them from the
recorded per-run reports so the text cannot drift from the data."""

import json
import statistics as st
from pathlib import Path

import pytest

from qbridge import deadlines

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "overhead"
README = (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def by_condition():
    meta = json.loads((FIX / "overhead.json").read_text(encoding="utf-8"))
    out: dict = {}
    for r in meta["runs"]:
        rep = json.loads((FIX / f"{r['run_id']}.report.json").read_text(encoding="utf-8"))
        out.setdefault(r["condition"], []).append((r["round"], rep))
    return {c: [rep for _, rep in sorted(v, key=lambda x: x[0])] for c, v in out.items()}


def test_six_interleaved_runs_per_condition(by_condition):
    assert {c: len(v) for c, v in by_condition.items()} == {"quiet": 6, "reporting": 6, "polling": 6}
    assert all(r["cycles"] == 999 for v in by_condition.values() for r in v)


def test_typical_cycle_is_the_same_as_the_readme_says(by_condition):
    med = {c: st.median(r["response_us"]["p50"] for r in v) / 1000 for c, v in by_condition.items()}
    mean = {c: st.mean(r["response_us"]["mean"] for r in v) / 1000 for c, v in by_condition.items()}
    assert "median response 1.083, 1.079 and 1.089 ms" in README
    assert [round(med[c], 3) for c in ("quiet", "reporting", "polling")] == [1.083, 1.079, 1.089]
    assert "mean 1.256, 1.249 and 1.254 ms" in README
    assert [round(mean[c], 3) for c in ("quiet", "reporting", "polling")] == [1.256, 1.249, 1.254]


def test_misses_only_with_qbridge_active_and_only_in_the_first_rounds(by_condition):
    misses = {c: [r["misses"] for r in v] for c, v in by_condition.items()}
    assert misses["quiet"] == [0] * 6
    assert misses["reporting"] == [0, 2, 4, 0, 0, 0]
    assert misses["polling"] == [5, 4, 0, 0, 0, 0]
    assert "(2, 4, 5 and 4)" in README


def test_every_miss_started_with_a_late_wakeup_not_preemption(by_condition):
    chains = [c for v in by_condition.values() for r in v for c in r["worst_chains"]]
    assert chains and all(c["root_cause"] == "latency" for c in chains)
    late = [c["root"]["latency_us"] / 1000 for c in chains]
    assert f"{min(late):.1f}-{max(late):.1f} ms late" in README         # 9.8-46.9
    assert max(c["root"]["preempted_us"] for c in chains) <= 220      # "at most 0.2 ms"


def test_the_verdict_tool_calls_it_inconclusive_not_worse(by_condition):
    for c in ("reporting", "polling"):
        assert deadlines.compare_sets(by_condition["quiet"], by_condition[c])["verdict"] == "inconclusive"
