"""Message-passing priority inversion (rtdemo -s msgpass), on data recorded
on the reference QNX 8.0 target on Nov 22, 2026."""

import gzip
import json
from pathlib import Path

from qbridge import analysis, traceconfirm
from qbridge.parsers import kev

FX = Path(__file__).parent / "fixtures"


def _diag(name):
    d = json.loads((FX / f"scenario_{name}.json").read_text(encoding="utf-8"))
    return d, analysis.diagnose(d["snapshots"], d["deadline_report"], d["timing_checks"])


def _trace(name):
    return kev.parse(gzip.decompress((FX / "traces" / f"{name}.txt.gz").read_bytes()).decode("utf-8"))


def test_fault_finds_only_the_msgpass_rule_with_high_confidence():
    d, res = _diag("msgpass_fault")
    assert d["args"] == ["-s", "msgpass"]
    assert [(f["rule"], f["confidence"]) for f in res["findings"]] == \
        [(analysis.RULE_MSGPASS_INVERSION, "high")]
    f = res["findings"][0]
    assert f["subject"]["server"]["thread_name"] == "S-server"
    assert f["subject"]["server"]["priority"] == 10
    assert [m["thread_name"] for m in f["subject"]["middle"]] == ["B-worker"]
    assert d["deadline_report"]["chain_root_causes"] == {"server_wait": d["deadline_report"]["miss_chains"]}


def test_fixed_finds_nothing_and_sees_the_server_at_the_clients_priority():
    _, res = _diag("msgpass_fixed")
    assert res["findings"] == []
    assert res["msgpass_inheritance_observed"]
    assert all(x["server_priority"] == 30 for x in res["msgpass_inheritance_observed"])


def test_msgpass_rule_does_not_fire_on_the_other_scenarios():
    for name in ("inversion", "overload_fault", "overload_fixed"):
        _, res = _diag(name)
        assert analysis.RULE_MSGPASS_INVERSION not in [f["rule"] for f in res["findings"]], name


def test_trace_confirms_the_fault_and_not_the_fix():
    _, res = _diag("msgpass_fault")
    f = res["findings"][0]
    assert traceconfirm.confirm(dict(f), _trace("msgpass_fault"))["status"] == "confirmed"
    assert traceconfirm.confirm(dict(f), _trace("msgpass_fixed"))["status"] == "not confirmed"


def test_recorded_live_results_match():
    for name in ("msgpass_fault", "msgpass_fixed"):
        d, res = _diag(name)
        key = lambda fs: [(f["rule"], f["confidence"]) for f in fs]
        assert key(res["findings"]) == key(d["result"]["findings"]), name


def test_report_chart_shows_the_wait_and_each_threads_priority():
    """On the recorded fault trace the chart draws the client's wait on the server
    in its own colour and prints the server's 10 and the client's 30 on the bars."""
    import re
    from qbridge import report
    _, res = _diag("msgpass_fault")
    tl = traceconfirm.confirm(dict(res["findings"][0]), _trace("msgpass_fault"))["timeline"]
    svg = report._trace_timeline(tl)
    assert 'class="st-msg"' in svg and "waiting for a server" in svg
    lanes = svg.split('class="lane"')[1:]           # one chunk per lane, in order
    prios = [re.findall(r'class="prio"[^>]*>(\d+)<', ln) for ln in lanes]
    names = [l["lane"] for l in tl["lanes"]]
    by = dict(zip(names, prios))
    assert by["client"] and set(by["client"]) == {"30"}
    assert "10" in by["server"]
    assert "20" in by["middle"]
    # a label only where the priority changes, never twice in a row
    assert all(a != b for p in prios for a, b in zip(p, p[1:]))
