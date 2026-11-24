"""Build small .kev files in memory for decoder edge cases.

Real traces (tests/fixtures/kev, examples/traces) cover what QNX writes; these
synthetic files cover what it rarely writes but a decoder must survive:
interlaced combine events, rollovers, truncation, orphans, big-endian files.
They follow the layout documented in <sys/trace.h> and seen in real files:
header text, system page, then buffers (TIME, BUFFER, events, BUFFER_END).
"""

from __future__ import annotations

import struct

S, CB, CC, CE = 0, 1, 2, 3
COOKIE = 0xE6E6E6E6


def hdr(cls: int, ev: int, cpu: int = 0, st: int = S, flags: int = 0) -> int:
    return (st << 30) | (cpu << 24) | flags | (cls << 10) | ev


def header(cpus: int = 2, cps: int = 1_000_000_000, little: bool = True, syspage: bytes = b"",
           extra: str = "") -> bytes:
    pairs = [("DATE", "Mon Nov 23 12:00:00 2026"), ("VER_MAJOR", "2"), ("VER_MINOR", "01"),
             ("LITTLE_ENDIAN" if little else "BIG_ENDIAN", "TRUE"),
             ("ENCODING", "16 byte events"), ("CYCLES_PER_SEC", str(cps)),
             ("CPU_NUM", str(cpus)), ("SYSNAME", "QNX"), ("SYS_RELEASE", "8.0.0"),
             ("MACHINE", "x86pc"), ("SYSPAGE_LEN", str(len(syspage)))]
    text = "TRACE_HEADER_BEGIN::" + "".join(f"TRACE_{k}::{v}" for k, v in pairs) + extra
    return text.encode("latin-1") + b"TRACE_HEADER_END::" + syspage


def buffer(slots: list[tuple[int, int, int, int]], cpu: int = 0, msb: int = 0, lsb: int = 0,
           seq: int = 1) -> list[tuple[int, int, int, int]]:
    """Frame event slots as one trace buffer."""
    n = len(slots) + 3
    return ([(hdr(1, 1, cpu), lsb, msb, lsb), (hdr(1, 2, cpu), lsb, seq, n)] + slots
            + [(hdr(1, 4, cpu), lsb, COOKIE, COOKIE)])


def kev(slots: list[tuple[int, int, int, int]], little: bool = True, **kw) -> bytes:
    e = "<" if little else ">"
    return header(little=little, **kw) + b"".join(struct.pack(f"{e}4I", *s) for s in slots)


def thread(state: int, t: int, pid: int, tid: int, prio: int | None = None, policy: int = 1,
           cpu: int = 0) -> list[tuple[int, int, int, int]]:
    """A THREAD class event: wide (combine, 2 slots) if prio is given, else fast."""
    if prio is None:
        return [(hdr(4, state, cpu), t, pid, tid)]
    return [(hdr(4, state, cpu, CB), t, pid, tid), (hdr(4, state, cpu, CE), t, prio, policy)]


def name_words(s: str, little: bool = True) -> list[int]:
    """A name as the target stores it: bytes in memory order, read as 32-bit words."""
    b = s.encode() + b"\x00"
    b += b"\x00" * (-len(b) % 4)
    if (len(b) // 4) % 2:
        b += b"\x00" * 4
    return list(struct.unpack(f"{'<' if little else '>'}{len(b) // 4}I", b))


def named(ev: int, t: int, a: int, b: int, name: str, cpu: int = 0,
          little: bool = True) -> list[tuple[int, int, int, int]]:
    """PROCCREATE_NAME (192) / PROCTHREAD_NAME (320): a, b, then the name."""
    words = [a, b] + name_words(name, little)
    pairs = [words[i:i + 2] for i in range(0, len(words), 2)]
    out = []
    for i, (x, y) in enumerate(pairs):
        st = CB if i == 0 else (CE if i == len(pairs) - 1 else CC)
        out.append((hdr(4, ev, cpu, st), t, x, y))
    return out
