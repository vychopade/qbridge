"""Make a smaller copy of a real .kev file for a test fixture.

    python tools/shrink_kev.py in.kev out.kev [--drop-kercall 65 ...]

Removes whole event slots of the given kernel calls (default: 65, ClockTime,
which a busy-waiting workload such as rtdemo calls hundreds of thousands of
times a second). Every other slot is copied byte for byte, in order, with the
header and system page unchanged. Combine events are dropped whole (all of
their slots), so the copy is still a well-formed trace; the CONTROL BUFFER
event counts are left as recorded (neither decoder relies on them).

Fixtures made this way say so in their README, and are re-decoded with the
SDP's traceprinter for the comparison: the copy is checked, not the original.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qbridge.parsers import kevbin  # noqa: E402


def shrink(data: bytes, drop: set[int]) -> tuple[bytes, int]:
    """The file is a run of buffers: CONTROL TIME, CONTROL BUFFER (data[2] = the
    buffer's slot count, itself included), events, CONTROL BUFFER_END. Slots are
    dropped inside each buffer and its count rewritten, which traceprinter needs."""
    header, pos = kevbin.parse_header(data)
    endian = "<" if header.get("LITTLE_ENDIAN", "").strip().upper() == "TRUE" else ">"
    start = pos + int(header.get("SYSPAGE_LEN", "0") or 0)
    out = bytearray(data[:start])
    removed = 0
    slot = struct.Struct(f"{endian}4I")
    nslots = (len(data) - start) // 16
    i = 0
    while i < nslots:
        n = kevbin.buffer_slots(data, start, i, nslots, slot)
        if n is None:
            raise SystemExit(f"slot {i}: not the start of a trace buffer")
        keep = []
        for k in range(i, i + n):
            h = slot.unpack_from(data, start + 16 * k)[0]
            cls, ev = (h >> 10) & 0x1F, h & 0x3FF
            if cls == kevbin.C_KERCALL and (ev & 0x7F) in drop:
                removed += 1
                continue
            keep.append(bytearray(data[start + 16 * k:start + 16 * k + 16]))
        h, d0, d1, _ = slot.unpack(bytes(keep[1]))
        keep[1] = bytearray(slot.pack(h, d0, d1, len(keep)))
        for b in keep:
            out += b
        i += n
    return bytes(out), removed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--drop-kercall", type=int, action="append", default=None)
    a = ap.parse_args()
    drop = set(a.drop_kercall or [65])
    data, n = shrink(Path(a.src).read_bytes(), drop)
    Path(a.dst).write_bytes(data)
    print(f"{a.dst}: removed {n} slots of kernel calls {sorted(drop)}, {len(data)} bytes")


if __name__ == "__main__":
    main()
