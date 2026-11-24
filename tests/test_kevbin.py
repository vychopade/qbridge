"""Native .kev decoding (parsers/kevbin.py) and its check against traceprinter.

Real data: tests/fixtures/kev/*.kev.gz are 0.25 s all-class traces recorded on the
QNX 8.0 target (Nov 24, 2026; rtdemo's four scenarios, fault and fixed), with the
ClockTime kernel calls removed by tools/shrink_kev.py, beside the SDP traceprinter's
output for exactly those files. examples/traces/*.kev.gz are the raw files behind the
traceprinter text examples there. Synthetic files (kevsynth.py) cover the damage and
corner cases real captures rarely show.
"""

import gzip
import json
import struct
from pathlib import Path

import pytest

import kevsynth as ks
from qbridge import cli, kevvalidate, tools, traceview
from qbridge.parsers import kev, kevbin, kevspec
from qbridge.policy import Policy
from qbridge.target import Target

ROOT = Path(__file__).parents[1]
KF = Path(__file__).parent / "fixtures" / "kev"
EX = ROOT / "examples" / "traces"
FIXTURES = sorted(p.name[:-len(".kev.gz")] for p in KF.glob("*.kev.gz"))


def _kev(name):
    return gzip.decompress((KF / f"{name}.kev.gz").read_bytes())


def _tp(name):
    return gzip.decompress((KF / f"{name}.traceprinter.txt.gz").read_bytes()).decode("utf-8")


# -- against traceprinter, on real traces ---------------------------------------------------

def test_there_are_real_fixtures():
    assert set(FIXTURES) >= {"inversion_fault", "inversion_fixed", "msgpass_fault",
                             "msgpass_fixed", "condvar_fault", "condvar_fixed", "overload_fast"}


@pytest.mark.parametrize("name", FIXTURES)
def test_every_event_matches_traceprinter(name):
    r = kevvalidate.compare(_kev(name), _tp(name))
    assert r["events_traceprinter"] > 2000
    assert r["matched"] == r["events_traceprinter"] == r["events_qbridge"]
    assert r["missing_in_qbridge"] == r["extra_in_qbridge"] == 0
    assert r["field_mismatch_events"] == 0 and r["fields_compared"] > 5000
    assert r["fields_equal"] == r["fields_compared"]
    assert r["timestamps_equal"] == r["timestamps_checked"] == r["matched"]
    assert r["order_disagreements"] == 0
    assert r["decoder_problems"] == 0


@pytest.mark.parametrize("name", ["overload", "inversion", "msgpass", "msgpass_fixed", "condvar"])
def test_raw_examples_give_the_same_trace_as_their_traceprinter_text(name):
    a = traceview.load_trace(EX / f"{name}.kev.gz")
    b = traceview.load_trace(EX / f"{name}.txt.gz")
    assert a.decoder == "qbridge native .kev decoder" and b.decoder == "traceprinter text"
    assert a.proc_names == b.proc_names and a.thread_names == b.thread_names
    assert a.duration_ms == pytest.approx(b.duration_ms)
    assert a.event_counts == b.event_counts
    assert a.spans.keys() == b.spans.keys()
    for k in a.spans:
        assert [(s.state, s.cpu, s.priority) for s in a.spans[k]] == \
               [(s.state, s.cpu, s.priority) for s in b.spans[k]]
        assert [s.start for s in a.spans[k]] == pytest.approx([s.start for s in b.spans[k]])


def test_header_and_stats_of_a_real_file():
    kf = kevbin.decode(_kev("msgpass_fault"))
    assert kf.little_endian and kf.cpus == 2 and kf.cycles_per_sec > 10**9
    assert kf.header["SYS_RELEASE"] == "8.0.0" and kf.header["ENCODING"] == "16 byte events"
    assert "-w" in kf.header["TRACELOGGER_ARGS"]
    assert kf.stats["buffers"] > 10 and kf.stats.get("framing_errors", 0) == 0
    assert kf.stats["events"] == len(kf.events)
    ts = [e["t"] for e in kf.events]
    assert ts == sorted(ts)
    assert {e["cpu"] for e in kf.events} == {0, 1}


def test_wide_and_fast_thread_events():
    wide = [e for e in kevbin.decode(_kev("inversion_fault")).events if e["ev"] == "THRUNNING"]
    fast = [e for e in kevbin.decode(_kev("overload_fast")).events if e["ev"] == "THRUNNING"]
    assert wide and all(e["mode"] == "wide" and set(e["f"]) == {"pid", "tid", "priority", "policy"}
                        for e in wide)
    assert fast and all(e["mode"] == "fast" and set(e["f"]) == {"pid", "tid"} for e in fast)
    tr = kev.parse_kev(_kev("overload_fast"))           # fast mode: no priorities in the trace
    assert {s.priority for v in tr.spans.values() for s in v} == {-1}


def test_mutex_and_message_events_are_decoded():
    ev = kevbin.decode(_kev("inversion_fault")).events
    locks = [e for e in ev if e["ev"] == "SYNC_MUTEX_LOCK/80" and e["cls"] == "KER_CALL"]
    assert locks and all(e["decoded"] and len(e["f"]["sync_p"]) == 18 for e in locks)  # 64-bit
    assert all({"sync_p", "count", "owner"} <= set(e["f"]) for e in locks)
    mp = kevbin.decode(_kev("msgpass_fault")).events
    names = {e["ev"] for e in mp if e["cls"] == "COMM"}
    assert {"SND_MESSAGE", "REC_MESSAGE", "REPLY_MESSAGE"} <= names
    snd = next(e for e in mp if e["ev"] == "SND_MESSAGE")
    assert snd["f"]["rcvid"].startswith("0x") and snd["f"]["pid"].isdigit()


def test_events_keep_their_provenance():
    data = _kev("msgpass_fault")
    kf = kevbin.decode(data)
    e = next(e for e in kf.events if e["ev"] == "PROCTHREAD_NAME")
    assert e["slots"] >= 2 and e["off"] % 16 == len(data) % 16
    h = struct.unpack_from("<I", data, e["off"])[0]
    assert (h >> 30) == ks.CB and (h & 0x3FF) == 320 and len(e["words"]) == 2 * e["slots"]


def test_disputed_layout_is_left_undecoded():
    assert (46, False) in kevbin.DISPUTED
    h = ks.hdr(2, 46 | 0x200)
    slots = ks.buffer([(h | (ks.CB << 30), 5, 1, 2), (h | (ks.CE << 30), 5, 3, 4)])
    e = next(e for e in kevbin.decode(ks.kev(slots)).events if e["cls"] == "KER_CALL")
    assert e["ev"] == "THREAD_CREATE/46" and not e["decoded"] and e["f"] == {}


def test_generated_spec_matches_the_sdp_numbers():
    n = kevspec.KERCALL_NAMES
    assert n[11] == "MSG_SENDV" and n[65] == "CLOCK_TIME" and n[80] == "SYNC_MUTEX_LOCK"
    lock64 = kevspec.KERCALL_LAYOUTS[(80, False, True)]["wide"]
    assert lock64 == [("sync_p", 64, "Q"), ("count", 32, "D"), ("owner", 32, "D")]


# -- synthetic files: structure, damage, corner cases -----------------------------------------

def _ev(data, **kw):
    return [e for e in kevbin.decode(data, **kw).events if e["cls"] != "CONTROL"]


def test_interlaced_combine_events_are_reassembled():
    a = ks.thread(1, 100, 7, 1, prio=30)             # wide THRUNNING, two slots
    b = ks.thread(2, 105, 7, 2)                      # fast THREADY in between
    slots = ks.buffer([a[0], b[0], a[1]], lsb=90)
    ev = _ev(ks.kev(slots))
    assert [(e["ev"], e["t"], e["f"]) for e in ev] == [
        ("THRUNNING", 100, {"pid": "7", "tid": "1", "priority": "30", "policy": "1"}),
        ("THREADY", 105, {"pid": "7", "tid": "2"})]


def test_names_with_spaces_and_multi_slot_combines():
    slots = ks.buffer(ks.named(192, 10, 1, 42, "/proc/boot/a long process name")
                      + ks.named(320, 11, 42, 3, "Entropy Collection Thread"))
    tr = kev.parse_kev(ks.kev(slots))
    assert tr.proc_names[42] == "/proc/boot/a long process name"
    assert tr.thread_names[(42, 3)] == "Entropy Collection Thread"


def test_text_parser_keeps_names_with_spaces_too():
    text = ("TRACE_CYCLES_PER_SEC:: 1000\n -- KERNEL EVENTS --\n"
            "t:0x00000001 CPU:00 PROCESS         :PROCTHREAD_NAME\n"
            "                       pid:1\n                       tid:2\n"
            "                      name:Signal Thread\n"
            "t:0x00000002 CPU:00 THREAD          :THRUNNING          pid:1 tid:2 priority:10 policy:1\n")
    assert kev.parse(text).thread_names[(1, 2)] == "Signal Thread"


def test_rollover_and_interlaced_events_from_before_it():
    slots = ks.buffer(ks.thread(1, 0xFFFFFFF0, 7, 1) + ks.thread(2, 0x10, 7, 1)
                      + ks.thread(5, 0xFFFFFFF8, 7, 2)      # logged late, before the rollover
                      + ks.thread(1, 0x20, 7, 2), msb=4, lsb=0xFFFFFF00)
    ev = _ev(ks.kev(slots))
    assert [e["t"] for e in ev] == [(4 << 32) | 0xFFFFFFF0, (4 << 32) | 0xFFFFFFF8,
                                    (5 << 32) | 0x10, (5 << 32) | 0x20]


def test_cpus_are_merged_by_time():
    cpu1 = ks.buffer(ks.thread(1, 50, 8, 1, cpu=1), cpu=1, lsb=40)
    cpu0 = ks.buffer(ks.thread(1, 30, 7, 1) + ks.thread(2, 60, 7, 1), cpu=0, lsb=20)
    ev = _ev(ks.kev(cpu1 + cpu0))
    assert [(e["cpu"], e["t"]) for e in ev] == [(0, 30), (1, 50), (0, 60)]


def test_big_endian_files_decode_the_same():
    def slots(little):
        return ks.buffer(ks.thread(1, 100, 7, 1, prio=30)
                         + ks.named(320, 101, 7, 1, "main thread", little=little))
    le = kevbin.decode(ks.kev(slots(True), little=True))
    be = kevbin.decode(ks.kev(slots(False), little=False))
    assert not be.little_endian
    strip = lambda kf: [(e["t"], e["ev"], e["f"]) for e in kf.events]  # noqa: E731
    assert strip(le) == strip(be)


def test_orphan_continuation_and_missing_end_are_reported():
    a = ks.thread(1, 100, 7, 1, prio=30)
    slots = ks.buffer([a[1]] + [a[0]] + ks.thread(2, 110, 7, 2))   # CE first, then a lone CB
    kf = kevbin.decode(ks.kev(slots))
    assert kf.stats["orphan_slots"] == 1 and kf.stats["incomplete_combine"] == 1
    assert [e["ev"] for e in kf.events if e["cls"] == "THREAD"] == ["THREADY"]
    assert any("orphan" in t for _, t in kf.problems) and any("without an end" in t for _, t in kf.problems)


def test_truncated_files_are_reported_not_misread():
    data = ks.kev(ks.buffer(ks.thread(1, 100, 7, 1) + ks.thread(2, 110, 7, 1)))
    kf = kevbin.decode(data[:-7])                      # cut inside the last slot
    assert kf.stats["truncated_tail_bytes"] == 9 and kf.stats["framing_errors"] == 1
    assert [e["ev"] for e in kf.events if e["cls"] == "THREAD"] == ["THRUNNING", "THREADY"]
    with pytest.raises(kevbin.KevError, match="TRACE_HEADER_END"):
        kevbin.decode(data[:40])
    with pytest.raises(kevbin.KevError, match="system page"):
        kevbin.decode(ks.header(syspage=b"x" * 64)[:-10])


def test_broken_framing_is_reported():
    slots = ks.buffer(ks.thread(1, 100, 7, 1))
    slots[-1] = (slots[-1][0], slots[-1][1], 0, 0)    # BUFFER_END without the cookie
    kf = kevbin.decode(ks.kev(slots))
    assert kf.stats["framing_errors"] == 1
    assert any("BUFFER_END without _TRACECOOKIE" in t for _, t in kf.problems)


@pytest.mark.parametrize("data, msg", [
    (b"hello", "TRACE_HEADER_BEGIN"),
    (ks.header().replace(b"TRACE_LITTLE_ENDIAN::TRUE", b""), "byte order"),
    (ks.header(cps=0), "CYCLES_PER_SEC"),
    (ks.header().replace(b"16 byte events", b"32 byte events"), "encoding"),
])
def test_not_a_trace(data, msg):
    with pytest.raises(kevbin.KevError, match=msg):
        kevbin.decode(data)


def test_unknown_events_are_kept_raw_and_flagged():
    slots = ks.buffer([(ks.hdr(6, 900), 5, 1, 2), (ks.hdr(5, 0x200 | 16), 6, 3, 4),
                       (ks.hdr(4, 40), 7, 1, 1), (ks.hdr(4, 1, cpu=5), 8, 1, 1)])
    kf = kevbin.decode(ks.kev(slots))
    ev = [e for e in kf.events if e["cls"] != "CONTROL"]
    assert [(e["cls"], e["ev"], e["decoded"]) for e in ev] == [
        ("USER", "EVENT900", False), ("SYSTEM", "IPI_64", False), ("THREAD", "TH?40", False),
        ("THREAD", "THRUNNING", True)]
    assert ev[0]["words"] == (1, 2)
    assert any("CPU 5" in t for _, t in kf.problems)


def test_injected_events_are_marked():
    slots = ks.buffer([(ks.hdr(4, 1, flags=kevbin.INJECTED), 5, 1, 2)])
    assert _ev(ks.kev(slots))[0].get("injected") is True


# -- in the workflow ---------------------------------------------------------------------------

def test_trace_file_reads_a_raw_kev_with_no_sdp(tmp_path, monkeypatch):
    def no_sdp():
        raise AssertionError("the SDP must not be needed")
    monkeypatch.setattr(tools, "_traceprinter", no_sdp)
    s = tools.Session(Target(host="192.0.2.1"), Policy())
    out = tools.trace_file(s, str(EX / "overload.kev.gz"), html_out=str(tmp_path / "o.html"))
    assert out["decoder"] == "qbridge native .kev decoder"
    assert out["decode"]["problems"] == 0 and out["decode"]["events"] == out["trace"]["events"]
    assert any(p["pattern"] == "cpu_preemption" for p in out["patterns"])


def test_traceprinter_stays_available_as_a_decoder(tmp_path, monkeypatch):
    text = gzip.decompress((EX / "overload.txt.gz").read_bytes()).decode("utf-8")

    def fake_run(argv, **kw):
        Path(argv[argv.index("-o") + 1]).write_text(text, encoding="utf-8")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(tools, "_traceprinter", lambda: "traceprinter")
    monkeypatch.setattr("subprocess.run", fake_run)
    tr = traceview.load_trace(EX / "overload.kev.gz", decoder="traceprinter")
    assert tr.decoder == "QNX traceprinter (SDP)" and tr.find_thread("A-control")
    with pytest.raises(ValueError, match="decoder"):
        traceview.load_trace(EX / "overload.kev.gz", decoder="magic")


def test_cli_trace_file_and_validate_kev(tmp_path, capsys):
    assert cli.main(["trace", "--file", str(EX / "msgpass.kev.gz"),
                     "--html", str(tmp_path / "m.html")]) == 0
    assert json.loads(capsys.readouterr().out)["decoder"] == "qbridge native .kev decoder"
    rep = tmp_path / "v.json"
    assert cli.main(["validate-kev", str(KF / "msgpass_fault.kev.gz"), "--out", str(rep)]) == 0
    capsys.readouterr()
    r = json.loads(rep.read_text(encoding="utf-8"))
    assert r["totals"]["files"] == 1 and r["totals"]["field_mismatch_events"] == 0
    assert r["by_type"]["COMM:SND_MESSAGE"]["fully_equal"] == r["by_type"]["COMM:SND_MESSAGE"]["traceprinter"]


def test_validate_kev_fails_when_the_decoders_disagree(tmp_path, capsys):
    k = tmp_path / "x.kev"
    k.write_bytes(_kev("overload_fast"))
    t = _tp("overload_fast").replace("pid:1 tid:1", "pid:1 tid:9", 1)
    (tmp_path / "x.txt").write_text(t, encoding="utf-8")
    assert cli.main(["validate-kev", str(k)]) == 1
    r = json.loads(capsys.readouterr().out)
    assert r["totals"]["field_mismatch_events"] == 1
    assert r["files"][0]["mismatch_samples"][0]["fields"] == [
        {"field": "tid", "qbridge": "1", "traceprinter": "9"}]
