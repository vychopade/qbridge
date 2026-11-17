from pathlib import Path

from qbridge import crash, tools
from qbridge.policy import Policy
from qbridge.target import Result, Target


def _report(fixture_text):
    return crash.parse_gdb(fixture_text("gdb_sensor_null.txt"))


def test_signal_and_program(fixture_text):
    r = _report(fixture_text)
    assert r["program"] == "/data/qbridge/sensor"
    assert (r["signal"], r["signo"], r["si_code"]) == ("SIGSEGV", 11, 1)
    assert r["fault_address"] == "0x8"
    # Addresses change every run (ASLR); the faulting IP must be frame 0's address.
    assert int(r["fault_ip"], 16) == int(r["frames"][0]["address"], 16)


def test_frames_with_args_and_locals(fixture_text):
    frames = _report(fixture_text)["frames"]
    assert [f["function"] for f in frames] == ["apply_calibration", "main"]
    top = frames[0]
    assert (top["file"], top["line"]) == ("sensor.c", 38)
    assert top["args"] == {"sensor_id": "7", "raw": "10"}
    assert top["locals"] == {"cal": "0x0"}
    main = frames[1]
    assert main["locals"]["sensors"] == "{1, 2, 3, 7}"
    assert main["locals"]["i"] == "3"


def test_source_and_threads(fixture_text):
    r = _report(fixture_text)
    assert "38\t    return (raw + cal->offset) * cal->scale;" in r["source"]
    assert r["thread_count"] == 1
    assert "all_threads" not in r


def test_summary_names_the_null_pointer(fixture_text):
    s = _report(fixture_text)["summary"]
    assert "SIGSEGV" in s and "apply_calibration() at sensor.c:38" in s
    assert "NULL pointer dereference (field at offset 8)" in s
    assert "cal" in s


def test_summary_for_non_null_fault():
    s = crash.summarise({"program": "/x", "signal": "SIGSEGV", "fault_address": "0xdeadbeef",
                         "frames": [{"function": "f", "file": "a.c", "line": 3, "args": {}, "locals": {}}]})
    assert "NULL" not in s and "0xdeadbeef" in s


def test_library_frame_without_source():
    lines = ["#0  0x00000051587f9b4a in strlen () from C:\\qnx\\lib\\libc.so.6",
             "No symbol table info available.",
             "#1  0x0000004995cc89cb in main (argc=1, argv=0x1) at m.c:5",
             "        s = 0x0"]
    frames = crash.parse_backtrace(lines)
    assert frames[0]["function"] == "strlen" and frames[0]["library"].endswith("libc.so.6")
    assert "file" not in frames[0]
    assert frames[1]["locals"] == {"s": "0x0"}


def test_multiline_struct_local_is_joined():
    lines = ["#0  f (p=0x1) at a.c:1",
             "        st = {a = 1,",
             "          b = 2}"]
    assert crash.parse_backtrace(lines)[0]["locals"]["st"] == "{a = 1, b = 2}"


def test_gdb_args_use_forward_slashes_and_markers():
    args = crash.gdb_args(Path(r"C:\c\x.core"), Path(r"C:\c\x.bin"), r"C:\qnx\target\qnx")
    joined = " ".join(args)
    assert "-batch" in args and "-nx" in args
    assert "core-file C:/c/x.core" in joined and "file C:/c/x.bin" in joined
    assert "C:/qnx/target/qnx/x86_64/lib" in joined
    assert "bt full" in args and f"echo {crash.MARK}backtrace\\n" in args


class StatTarget(Target):
    def __init__(self, out):
        super().__init__(host="192.0.2.10")
        self.out = out

    def run(self, argv, timeout=60):
        return Result(" ".join(argv), 0, self.out, "")


def test_list_cores_from_recorded_stat():
    t = StatTarget("/data/var/dumper/sensor.core|1658880|1794887056\n"
                   "/data/var/dumper/older.core|100|1794880000\n")
    cores = crash.list_cores(t)
    assert [c["program"] for c in cores] == ["sensor", "older"]
    assert cores[0] == {"path": "/data/var/dumper/sensor.core", "program": "sensor",
                        "bytes": 1658880, "time": 1794887056}


def test_no_cores_message():
    r = crash.analyse(StatTarget(""))
    assert r["found"] is False and "/data/var/dumper" in r["message"]


def test_run_reports_signal_from_exit_code():
    class Segv(Target):
        def run(self, argv, timeout=60):
            return Result(" ".join(argv), 139, "sensor 1: 10.710\n", "")

    s = tools.Session(Segv(host="192.0.2.10"), Policy(allow_write=True))
    out = tools.run(s, "sensor")
    assert out["signal"] == "SIGSEGV" and "qnx_crash" in out["hint"]
    assert out["timed_out"] is False
