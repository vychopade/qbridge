import io
import json

from qbridge import server, tools
from qbridge.policy import Policy
from qbridge.target import Result, Target


class FakeTarget(Target):
    """Answers commands from recorded fixtures instead of a real target."""

    def __init__(self, outputs: dict[str, str]):
        super().__init__(host="192.0.2.10")
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def run(self, argv, timeout=60):
        self.calls.append(list(argv))
        return Result(" ".join(argv), 0, self.outputs.get(argv[0], ""), "")


def _talk(session, *messages):
    out = io.StringIO()
    server_in = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
    srv = server.Server(session)
    for line in server_in:
        reply = srv.handle(json.loads(line))
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
    return [json.loads(l) for l in out.getvalue().splitlines()]


def test_initialize_and_list_tools():
    s = tools.Session(FakeTarget({}), Policy())
    init, listed = _talk(
        s,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "qbridge"
    names = {t["name"] for t in listed["result"]["tools"]}
    assert {"qnx_ps", "qnx_logs", "qnx_build", "qnx_deploy", "qnx_run"} <= names
    run_tool = next(t for t in listed["result"]["tools"] if t["name"] == "qnx_run")
    assert run_tool["annotations"]["readOnlyHint"] is False


def test_unknown_protocol_version_gets_latest():
    s = tools.Session(FakeTarget({}), Policy())
    (init,) = _talk(s, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "1999-01-01"}})
    assert init["result"]["protocolVersion"] == server.SUPPORTED_PROTOCOLS[0]


def test_ps_tool_uses_recorded_pidin(fixture_text):
    t = FakeTarget({"pidin": fixture_text("pidin_threads.txt")})
    s = tools.Session(t, Policy())
    (reply,) = _talk(s, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "qnx_ps", "arguments": {"process": "ksh"}}})
    res = reply["result"]
    assert res["isError"] is False
    assert len(res["structuredContent"]["processes"]) == 4
    assert res["structuredContent"]["blocked_on"][0]["to_name"] == "devc-con"


def test_run_refused_when_read_only():
    t = FakeTarget({})
    s = tools.Session(t, Policy(allow_write=False))
    (reply,) = _talk(s, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "qnx_run", "arguments": {"path": "hello"}}})
    assert reply["result"]["isError"] is True
    assert "read-only" in reply["result"]["content"][0]["text"]
    assert t.calls == []  # nothing reached the target


def test_run_wraps_in_timeout_and_stays_in_deploy_root():
    t = FakeTarget({"timeout": "hi\n"})
    s = tools.Session(t, Policy(allow_write=True))
    out = tools.run(s, "hello", ["a b"], timeout=5)
    assert t.calls[-1] == ["timeout", "5", "/data/qbridge/hello", "a b"]
    assert out["stdout"] == "hi\n" and out["timed_out"] is False


def test_unknown_tool_and_method():
    s = tools.Session(FakeTarget({}), Policy())
    bad_tool, bad_method = _talk(
        s,
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}},
        {"jsonrpc": "2.0", "id": 6, "method": "resources/list"},
    )
    assert bad_tool["error"]["code"] == -32602
    assert bad_method["error"]["code"] == -32601


def test_cli_accepts_allow_write_after_serve():
    from qbridge.cli import build_parser
    a = build_parser().parse_args(["serve", "--allow-write"])
    assert a.serve_allow_write and not a.allow_write
    b = build_parser().parse_args(["--allow-write", "serve"])
    assert b.allow_write and not b.serve_allow_write


def test_cli_measure_options_are_not_program_args():
    from qbridge.cli import build_parser
    a = build_parser().parse_args(["measure", "rtdemo", "--seconds", "5", "--label", "base"])
    assert (a.program, a.seconds, a.label, a.args) == ("rtdemo", 5, "base", [])
    b = build_parser().parse_args(["measure", "rtdemo", "--seconds", "5", "--", "-c", "1"])
    assert b.args == ["-c", "1"] and b.seconds == 5


def test_serve_loop_reads_lines():
    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n\n")
    stdout = io.StringIO()
    server.serve(stdin=stdin, stdout=stdout)
    assert json.loads(stdout.getvalue()) == {"jsonrpc": "2.0", "id": 1, "result": {}}
