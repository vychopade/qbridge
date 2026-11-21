"""Audit log: every call recorded, refusals marked, hash chain detects edits."""

import json

import pytest

from qbridge import audit, cli, server, tools
from qbridge.policy import Policy
from qbridge.target import Result, Target


def _lines():
    return audit.log_path().read_text(encoding="utf-8").splitlines()


def test_chain_verifies_and_detects_edits():
    for i in range(3):
        audit.record("cli", "qnx_ps", {"process": f"p{i}"}, False, "ok", target="192.0.2.10")
    v = audit.verify()
    assert v["entries"] == 3 and v["intact"] is True
    entries = audit.read()
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert entries[0]["prev"] == audit.GENESIS and entries[1]["prev"] == entries[0]["hash"]

    lines = _lines()
    e = json.loads(lines[1])
    e["outcome"] = "refused"                          # edit an old entry
    lines[1] = json.dumps(e, sort_keys=True)
    audit.log_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    v = audit.verify()
    assert v["intact"] is False and "contents changed" in v["problems"][0]


def test_deleting_an_entry_is_detected():
    for i in range(3):
        audit.record("cli", "qnx_ps", {}, False, "ok")
    lines = _lines()
    audit.log_path().write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
    v = audit.verify()
    assert v["intact"] is False
    assert any("does not link" in p for p in v["problems"])
    assert any("sequence jumps" in p for p in v["problems"])


def test_long_arguments_are_clipped_and_details_kept():
    e = audit.record("mcp", "qnx_experiment_propose", {"replace": "x" * 5000}, False, "ok",
                     result={"run_id": "r1", "remote": "/data/qbridge/a", "stdout": "big" * 999,
                             "processes": [{"pid": 7}], "diff_sha256": "ab" * 32})
    assert len(e["args"]["replace"]) < 2100 and "more chars" in e["args"]["replace"]
    assert e["details"] == {"run_id": "r1", "remote": "/data/qbridge/a", "pids": [7],
                            "diff_sha256": "ab" * 32}
    assert "stdout" not in e["details"]


class Fake(Target):
    def run(self, argv, timeout=60):
        return Result(" ".join(argv), 0, "", "")


def test_mcp_calls_are_recorded_with_outcome():
    srv = server.Server(tools.Session(Fake(host="192.0.2.10"), Policy(allow_write=False)))
    srv.call_tool("qnx_run", {"path": "rtdemo"})           # refused: read-only
    srv.call_tool("qnx_audit", {"limit": 5})               # ok, read-only
    a, b = audit.read()
    assert (a["interface"], a["tool"], a["outcome"], a["changes_target"]) == \
        ("mcp", "qnx_run", "refused", True)
    assert "read-only" in a["error"] and a["args"] == {"path": "rtdemo"}
    assert (b["tool"], b["outcome"], b["changes_target"]) == ("qnx_audit", "ok", False)


def test_cli_refusal_is_recorded(capsys):
    assert cli.main(["deploy", "some.bin"]) == 1        # read-only: refused before any ssh
    (e,) = audit.read()
    assert (e["interface"], e["tool"], e["outcome"], e["changes_target"]) == \
        ("cli", "qnx_deploy", "refused", True)
    assert e["args"]["local_path"] == "some.bin"


def test_audit_tool_reports_and_verifies():
    audit.record("cli", "qnx_deploy", {}, True, "ok", result={"remote": "/data/qbridge/x"})
    audit.record("mcp", "qnx_kill", {"pid": 1}, True, "refused", error="PolicyError: outside")
    out = tools.audit_log(tools.Session(Fake(host="192.0.2.10"), Policy()), 10, True)
    assert out["recent"] == {"total": 2, "changed_target": 1, "ok": 1, "refused": 1, "error": 0}
    assert out["verify"]["intact"] is True


def test_every_cli_command_maps_to_a_tool_name():
    p = cli.build_parser()
    for argv in (["info"], ["ps"], ["measure", "rtdemo"], ["diagnose"], ["audit"],
                 ["experiment", "run", "x"], ["experiment", "approve", "x"], ["image", "install"]):
        name, _ = cli._audit_name(p.parse_args(argv))
        assert name.startswith(("qnx_", "image_")), argv
    assert cli._audit_name(p.parse_args(["deploy", "x"])) == ("qnx_deploy", True)
    assert cli._audit_name(p.parse_args(["experiment", "approve", "x"])) == \
        ("qnx_experiment_approve", False)
