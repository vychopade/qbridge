"""The codelab (docs/codelab) and its example must stay in step: the code it
quotes exists in the example, the outputs it shows match what the example
prints, and (when the QNX SDP is installed) the example builds without
warnings. The outputs themselves were recorded on the QNX 8.0 target."""

import re
from pathlib import Path

import pytest

from qbridge import build, qnxenv

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "docs" / "codelab"
SRC = {p.name: p.read_text(encoding="utf-8") for p in (LAB / "livedata").glob("*.c")}
DOC = (LAB / "README.md").read_text(encoding="utf-8")


def _c_blocks(text):
    return re.findall(r"```c\n(.*?)```", text, re.S)


def test_both_steps_exist():
    assert set(SRC) == {"livedata1.c", "livedata2.c"}


def test_every_function_and_macro_quoted_is_used_in_the_example():
    code = "\n".join(SRC.values())
    quoted = set()
    for block in _c_blocks(DOC):
        quoted |= set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", block))
    quoted -= {"if", "for", "sizeof", "return"}
    missing = sorted(n for n in quoted if n not in code)
    assert not missing, missing


def _statements(code):
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    code = re.sub(r"^\s*#.*$", " ", code, flags=re.M)
    return [" ".join(p.split()) for p in re.split(r"[;{}]", code)]


def test_quoted_statements_appear_in_the_example():
    """Every statement the codelab quotes must be in the example as written
    (statements the codelab shortens contain '...', or put an if and its
    return on one line, and are skipped)."""
    source = " ".join(" ".join(_statements(src)) for src in SRC.values())
    checked = 0
    for block in _c_blocks(DOC):
        for st in _statements(block):
            if len(st) < 6 or "..." in st or (st.startswith("if (") and "return" in st):
                continue
            checked += 1
            assert st in source, f"not in the example: {st!r}"
    assert checked > 30


def test_shown_outputs_match_the_programs_formats():
    shown = re.findall(r"uptime_s: (\d+\.\d{3})\nopens: (\d+)\n", DOC)
    assert len(shown) >= 2
    assert 'uptime_s: %lld.%03ld\\nopens: %u\\n' in SRC["livedata1.c"]
    for name in SRC:
        assert f'printf("{name[:-2]}: serving /dev/livedata/uptime' in SRC[name]
        assert f"{name[:-2]}: serving /dev/livedata/uptime" in DOC
    # The message file starts as this text, and ls -l showed its 20 bytes.
    first = "nothing written yet\n"
    assert 'message[256] = "nothing written yet\\n"' in SRC["livedata2.c"]
    assert f"message_len = {len(first)};" in SRC["livedata2.c"]
    assert f"root root {len(first)} " in DOC
    final = "hello from the codelab\nsecond line\n"
    assert final in DOC and f"root root {len(final)} " in DOC


def test_step2_is_step1_plus_the_message_file():
    one, two = SRC["livedata1.c"], SRC["livedata2.c"]
    for part in ("live_read(", "live_ocb_calloc(", "dispatch_block(ctp)", '"/dev/livedata/uptime"'):
        assert part in one and part in two
    assert "live_write" not in one and "io_funcs.write = live_write;" in two
    assert '"/dev/livedata/message"' in two


def _sdp_available():
    try:
        return build._which("qcc", qnxenv.load()) is not None
    except Exception:
        return False


@pytest.mark.skipif(not _sdp_available(), reason="QNX SDP (qcc) not installed")
def test_example_builds_without_warnings():
    r = build.build(LAB / "livedata", make_target="all")
    assert r.ok, r.log_tail if hasattr(r, "log_tail") else r
    assert r.errors == [] and r.warnings == []
    assert (LAB / "livedata" / "livedata1").is_file() and (LAB / "livedata" / "livedata2").is_file()
