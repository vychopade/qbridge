import pytest

from qbridge.parsers import slog2


def _entries(fixture_text):
    return slog2.parse(fixture_text("slog2info_fSt.txt"))


def test_every_line_is_a_record(fixture_text):
    text = fixture_text("slog2info_fSt.txt")
    lines = [l for l in text.splitlines() if l.strip()]
    entries = _entries(fixture_text)
    assert len(entries) == len(lines)
    assert all("\n" not in e.message for e in entries)


def test_first_entry_fields(fixture_text):
    e = _entries(fixture_text)[0]
    assert e.time == pytest.approx(1794886003.418)
    assert (e.process, e.pid, e.buffer) == ("pci_server", 28676, "pci_log")
    assert (e.tid, e.code, e.severity) == (1, 0, "INFO")
    assert e.message.startswith("INFO ,1,-10,0 [28676:1]:")


def test_bufset_with_index_suffix(fixture_text):
    # Recorded line: "random.155660..0  low*  1  0  SLOG2_NOTICE  random: Failed to open seed file ..."
    e = next(x for x in _entries(fixture_text) if x.process == "random" and x.buffer == "low")
    assert (e.pid, e.severity) == (155660, "NOTICE")
    assert "Failed to open seed file" in e.message


def test_filter_by_severity_keeps_worse(fixture_text):
    out = slog2.filter_entries(_entries(fixture_text), min_severity="warning")
    assert out
    assert all(e.severity in ("SHUTDOWN", "CRITICAL", "ERROR", "WARNING") for e in out)


def test_filter_by_process_accepts_dash_or_underscore(fixture_text):
    a = slog2.filter_entries(_entries(fixture_text), process="devb-eide")
    b = slog2.filter_entries(_entries(fixture_text), process="devb_eide")
    assert a and a == b
    assert all(e.pid == 90123 for e in a)


def test_filter_since_grep_and_limit(fixture_text):
    es = _entries(fixture_text)
    out = slog2.filter_entries(es, since=1794886004.2, contains="PCI WARN", limit=1)
    assert len(out) == 1
    assert out[0].time >= 1794886004.2 and "PCI WARN" in out[0].message


def test_unknown_severity_rejected():
    with pytest.raises(ValueError):
        slog2.filter_entries([], min_severity="loud")
