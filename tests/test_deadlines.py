"""Deadline statistics and miss attribution, on rtdemo runs recorded on the target."""

import json

import pytest

from qbridge import deadlines, runs, tools
from qbridge.policy import Policy, PolicyError
from qbridge.target import Result, Target

DEADLINE = 10000.0


def _report(fixture_text, name):
    samples = deadlines.parse_samples_csv(fixture_text(f"{name}_samples.csv"))
    return deadlines.analyse(samples, DEADLINE, 10000.0, "A-control")


def _last_summary(fixture_text, name):
    return json.loads(fixture_text(f"{name}_summary.json"))[-1]


@pytest.mark.parametrize("name", ["rtdemo", "rtdemo_fixed"])
def test_host_statistics_agree_with_the_program(fixture_text, name):
    rep = _report(fixture_text, name)
    summ = _last_summary(fixture_text, name)
    check = deadlines.cross_check(rep, summ)
    assert check == {"checked": True, "agrees": True, "problems": []}
    # the percentile rule matches rtdemo's too
    assert rep["response_us"]["p99"] == pytest.approx(summ["response"]["p99_us"], abs=1)
    assert rep["parts_us"]["lock_wait"]["max"] == pytest.approx(summ["lock_wait"]["max_us"], abs=1)


def test_fault_run_is_attributed_to_the_lock(fixture_text):
    rep = _report(fixture_text, "rtdemo")
    assert rep["misses"] == _last_summary(fixture_text, "rtdemo")["misses"]
    # every run of misses starts with a long lock wait ...
    assert set(rep["chain_root_causes"]) == {"lock_wait"}
    # ... and most individual misses after the first are knock-on backlog
    assert rep["per_miss_primary_cause"]["backlog"] > rep["per_miss_primary_cause"]["lock_wait"]
    assert rep["longest_chain"] >= 3
    worst = rep["worst_chains"][0]
    assert worst["root"]["lock_wait_us"] > 30000
    assert "waiting for a lock" in rep["summary"]


def test_fixed_run_has_no_misses_and_compare_says_improved(fixture_text):
    before = _report(fixture_text, "rtdemo")
    after = _report(fixture_text, "rtdemo_fixed")
    assert after["misses"] == 0 and after["miss_chains"] == 0
    cmp = deadlines.compare(before, after)
    assert cmp["verdict"] == "improved"
    assert cmp["misses"]["before"] > 250 and cmp["misses"]["after"] == 0
    assert cmp["worst_response_us"]["change"] < -25000
    assert "does not prove" in cmp["explanation"]


def test_parts_are_separate_and_preempted_is_derived():
    s = deadlines.Sample(release=0, response=12000, backlog=1000, latency=2000,
                         lock_wait=3000, exec=1000)
    assert s.preempted == 5000
    # never negative, even with clock rounding
    assert deadlines.Sample(release=0, response=999, exec=1000).preempted == 0


def _cycle(release, response, **parts):
    parts.setdefault("exec", 1000)
    return deadlines.Sample(release=release, response=response, **parts)


def test_attribution_picks_the_largest_new_cause():
    samples = [_cycle(i * 10000, 1100) for i in range(20)]
    samples[3] = _cycle(30000, 15000, latency=13000)                 # late wake-up
    samples[8] = _cycle(80000, 14000, exec=13000)                    # ran long
    samples[12] = _cycle(120000, 16000, lock_wait=2000)              # preempted ~13 ms
    rep = deadlines.analyse(samples, DEADLINE)
    assert rep["misses"] == 3 and rep["miss_chains"] == 3
    assert rep["chain_root_causes"] == {"latency": 1, "exec": 1, "preempted": 1}


def test_chain_root_is_the_first_miss():
    samples = [_cycle(i * 10000, 1100) for i in range(10)]
    samples[2] = _cycle(20000, 30000, lock_wait=28000)
    samples[3] = _cycle(30000, 21000, backlog=20000)
    samples[4] = _cycle(40000, 11500, backlog=10500)
    rep = deadlines.analyse(samples, DEADLINE)
    assert rep["misses"] == 3 and rep["miss_chains"] == 1 and rep["longest_chain"] == 3
    assert rep["chain_root_causes"] == {"lock_wait": 1}
    assert rep["per_miss_primary_cause"] == {"lock_wait": 1, "backlog": 2}


def test_compare_inconclusive_and_worse():
    base = [_cycle(i * 10000, 1100) for i in range(10)]
    a = deadlines.analyse(base[:5] + [_cycle(50000, 12000, latency=11000)] + base[6:], DEADLINE)
    b = deadlines.analyse(base[:5] + [_cycle(50000, 20000, latency=19000)] + base[6:], DEADLINE)
    assert deadlines.compare(a, b)["verdict"] == "inconclusive"   # same misses, worse max
    c = deadlines.analyse([_cycle(i * 10000, 25000, latency=24000) for i in range(10)], DEADLINE)
    assert deadlines.compare(a, c)["verdict"] == "worse"


def test_csv_with_only_required_columns():
    s = deadlines.parse_samples_csv("release_us,response_us\n0,500\n10000,12000\n")
    assert len(s) == 2 and s[1].response == 12000 and s[1].lock_wait == 0
    rep = deadlines.analyse(s, DEADLINE)
    assert rep["misses"] == 1 and rep["chain_root_causes"] == {"preempted": 1}


def test_empty_stats():
    assert deadlines.stats([])["count"] == 0
    assert deadlines.analyse([], DEADLINE)["summary"].endswith("no cycles recorded.")


# -- the measure tool, against a fake target replaying recorded output ----------------

class Replay(Target):
    def __init__(self, summary_line, csv_text):
        super().__init__(host="192.0.2.10")
        self.summary_line, self.csv_text, self.calls = summary_line, csv_text, []

    def run(self, argv, timeout=60):
        self.calls.append(list(argv))
        if argv[0] == "openssl":
            return Result("", 0, "SHA2-256(/data/qbridge/rtdemo)= " + "ab" * 32 + "\n", "")
        if argv[0] == "timeout":
            return Result(" ".join(argv), 0, "starting\n" + self.summary_line + "\n", "")
        if argv[0] == "cat":
            return Result("cat", 0, self.csv_text, "")
        return Result(" ".join(argv), 0, "", "")


def test_measure_runs_fetches_analyses_and_saves(fixture_text, tmp_path, monkeypatch):
    monkeypatch.setenv("QBRIDGE_HOME", str(tmp_path))
    summ = _last_summary(fixture_text, "rtdemo")
    t = Replay(json.dumps(summ), fixture_text("rtdemo_samples.csv"))
    out = tools.measure(tools.Session(t, Policy(allow_write=True)), "rtdemo", seconds=10,
                        label="baseline")
    run_argv = next(c for c in t.calls if c[0] == "timeout")
    assert run_argv[2:] == ["/data/qbridge/rtdemo", "-d", "10", "-o",
                            "/data/qbridge/rtdemo.samples.csv"]
    assert out["cross_check"]["agrees"] is True
    assert out["binary_sha256"] == "ab" * 32
    assert out["report"]["misses"] == summ["misses"]

    saved = runs.load(out["run_id"])
    assert saved["meta"]["label"] == "baseline" and saved["meta"]["binary_sha256"] == "ab" * 32
    assert saved["report"]["misses"] == summ["misses"]
    assert (tmp_path / "runs" / out["run_id"] / "samples.csv").read_text(encoding="utf-8") \
        == fixture_text("rtdemo_samples.csv")
    listed = runs.list_runs()
    assert listed[0]["run_id"] == out["run_id"] and listed[0]["misses"] == summ["misses"]


def test_measure_needs_write_access(tmp_path, monkeypatch):
    monkeypatch.setenv("QBRIDGE_HOME", str(tmp_path))
    with pytest.raises(PolicyError):
        tools.measure(tools.Session(Replay("{}", ""), Policy()), "rtdemo")


def test_load_rejects_path_tricks(tmp_path, monkeypatch):
    monkeypatch.setenv("QBRIDGE_HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        runs.load("../../etc")
