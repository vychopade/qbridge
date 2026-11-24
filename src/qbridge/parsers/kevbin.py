"""Decode a raw QNX 8.0 kernel trace (.kev, as written by tracelogger) without
the SDP: no traceprinter, no libtraceparser, any host OS.

Everything here follows the QNX documentation shipped with the SDP:
  - <sys/trace.h>: traceevent_t (a 32-bit header and three 32-bit data
    words), the header bit fields (_NTO_TRACE_GETCPU, _NTO_TRACE_GETEVENT_C,
    _NTO_TRACE_GETEVENT, _TRACE_GET_STRUCT), the internal class numbers
    (_TRACE_*_C), the process/thread event numbers (_TRACE_PR_TH_*), control
    events and _TRACECOOKIE;
  - <sys/states.h>: thread state numbers (THREAD class event = new state);
  - the System Analysis Toolkit guide: "Simple and combine events" (a combine
    event spans slots marked begin/continue/end, all carrying the same
    timestamp, possibly interlaced with other events), "Timestamps" (data[0]
    is the low 32 bits of the clock; CONTROL TIME carries the high 32 bits and
    is emitted on rollover and at each buffer), and the "Current Trace Events
    and Data" table (fields per event in fast and wide mode; kevspec.py is
    generated from it).

File layout, as found in QNX 8.0 tracelogger output and checked against
traceprinter on real captures: a text header of TRACE_<KEY>::<value> pairs from
TRACE_HEADER_BEGIN:: to TRACE_HEADER_END::, then TRACE_SYSPAGE_LEN bytes of
system page, then 16-byte event slots to the end of the file.

Events whose fields the documentation describes are decoded field by field
with traceprinter's field names (so the same analysis runs on either input);
the rest keep their class, name, time, CPU and raw data words, with
decoded=False. Nothing is guessed: an event that does not match its
documented layout is kept raw and counted as such.
"""

from __future__ import annotations

import re
import struct
from collections import Counter
from dataclasses import dataclass, field

from . import kevspec

MAGIC = b"TRACE_HEADER_BEGIN::"
HEADER_END = b"TRACE_HEADER_END::"
SLOT = 16
COOKIE = 0xE6E6E6E6

# _TRACE_GET_STRUCT
S_SIMPLE, S_BEGIN, S_CONT, S_END = 0, 1, 2, 3

# Internal classes (_TRACE_*_C >> 10)
C_EMPTY, C_CONTROL, C_KERCALL, C_INT, C_PR_TH, C_SYSTEM, C_USER, C_COMM = range(8)
C_QUIP, C_SEC, C_QVM, C_PMU = 8, 9, 10, 11
CLASS_LABELS = {C_CONTROL: "CONTROL", C_INT: "INTERRUPT", C_SYSTEM: "SYSTEM", C_USER: "USER",
                C_COMM: "COMM", C_QUIP: "QUIP", C_SEC: "SECURITY", C_QVM: "QVM", C_PMU: "PMU"}

# <sys/states.h>; traceprinter prints "TH" + name.
STATES = {0: "DEAD", 1: "RUNNING", 2: "READY", 3: "STOPPED", 4: "SEND", 5: "RECEIVE", 6: "REPLY",
          7: "MQ_SEND", 8: "MQ_RECEIVE", 9: "WAITPAGE", 10: "SIGSUSPEND", 11: "SIGWAITINFO",
          12: "NANOSLEEP", 13: "MUTEX", 14: "CONDVAR", 15: "JOIN", 16: "INTR", 17: "SEM",
          18: "WAITCTX", 19: "RWLOCK_READ", 20: "RWLOCK_WRITE", 21: "BARRIER", 22: "PIPE",
          24: "CREATE", 25: "DESTROY", 26: "MUON_MUTEX", 27: "TRACEBUFFER", 28: "INTR_ATTACH_EV",
          29: "TIMER_DELEGATE", 30: "SEND_NOTIFY"}
THREAD_FIELDS = ("pid", "tid", "priority", "policy")      # wide; fast has pid, tid

# _TRACE_PR_TH_*_P and _TRACE_PR_TH_NAME_T (multiples of 64)
PROCESS_EVENTS = {64: ("PROCCREATE", ("ppid", "pid")),
                  128: ("PROCDESTROY", ("ppid", "pid")),
                  192: ("PROCCREATE_NAME", ("ppid", "pid", "name")),
                  256: ("PROCDESTROY_NAME", ("ppid", "pid", "name")),
                  320: ("PROCTHREAD_NAME", ("pid", "tid", "name"))}

CONTROL_EVENTS = {1: "TIME", 2: "BUFFER", 4: "BUFFER_END", 8: "DROPPED"}

# _NTO_TRACE_COMM_*: traceprinter label and documented fields
COMM_EVENTS = {0: ("SND_MESSAGE", ("rcvid", "pid")), 1: ("SND_PULSE", ("scoid", "pid")),
               2: ("REC_MESSAGE", ("rcvid", "pid")), 3: ("REC_PULSE", ("scoid", "pid")),
               4: ("SND_PULSE_EXE", ("scoid", "pid")), 5: ("SND_PULSE_DIS", ("scoid", "pid")),
               6: ("SND_PULSE_DEA", ("scoid", "pid")), 7: ("SND_PULSE_UN", ("scoid", "pid")),
               8: ("SND_PULSE_QUN", ("scoid", "pid")),
               9: ("SIGNAL", ("si_signo", "si_code", "si_errno")),
               10: ("REPLY_MESSAGE", ("tid", "pid")), 11: ("MSG_ERROR", ("tid", "pid"))}
COMM_HEX = {"rcvid", "scoid"}

# _NTO_TRACE_SYS_* (low 9 bits; 0x200 = 64-bit variant). Labels as traceprinter prints
# them where seen in real captures; the rest use the <sys/trace.h> name.
SYSTEM_EVENTS = {1: "RESERVED", 2: "PATHMGR_OPEN", 3: "APS_NAME", 4: "APS_BUDGETS", 5: "APS_BNKR",
                 6: "MMAP", 7: "MUNMAP", 8: "MAPNAME", 9: "ADDRESS", 10: "FUNC_ENTER",
                 11: "FUNC_EXIT", 12: "SLOG2", 13: "DEFRAG_START", 14: "RUNSTATE", 15: "POWER",
                 16: "IPI", 17: "PAGEWAIT", 18: "TIMER", 19: "DEFRAG_END", 20: "PROFILE",
                 21: "MAPNAME_64", 22: "APS_PSTATS", 23: "APS_OSTATS", 24: "APS_INFO",
                 25: "APS_JOIN", 26: "APS_THREAD", 27: "APS_PROCESS", 28: "SCHED_CONF",
                 29: "IST_ATTACH", 30: "IST_DETACH", 31: "PCTRACE", 32: "SEND_IPI"}
KERCALL64 = 0x200
INJECTED = 0x10000                       # _NTO_TRACE_PROCESS_INJECTED

# Kernel call layouts where traceprinter's output and the documented table disagree on
# real QNX 8.0 captures (checked by kevvalidate): the fields are left undecoded rather
# than picking a side. (call number, is_exit)
DISPUTED = {(46, False)}                 # ThreadCreate entry: traceprinter is one word off


class KevError(ValueError):
    """Not a .kev file, or one too damaged to decode."""


@dataclass
class KevFile:
    header: dict
    little_endian: bool
    cycles_per_sec: int
    cpus: int
    events: list                        # dicts, sorted by time (see decode())
    stats: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)   # [(file offset, text)], first 50


def parse_header(data: bytes) -> tuple[dict, int]:
    """TRACE_<KEY>::<value> pairs -> ({KEY: value}, offset just past the header)."""
    if not data.startswith(MAGIC):
        raise KevError("not a QNX kernel trace: the file does not start with TRACE_HEADER_BEGIN::")
    end = data.find(HEADER_END)
    if end < 0:
        raise KevError("truncated .kev header: TRACE_HEADER_END:: not found")
    text = data[:end].decode("latin-1")
    keys = list(re.finditer(r"TRACE_([A-Z0-9_]+)::", text))
    header = {}
    for i, m in enumerate(keys):
        stop = keys[i + 1].start() if i + 1 < len(keys) else len(text)
        header[m[1]] = text[m.end():stop].rstrip("\x00")
    header.pop("HEADER_BEGIN", None)
    return header, end + len(HEADER_END)


def buffer_slots(data: bytes, start: int, i: int, nslots: int, slot: struct.Struct) -> int | None:
    """If slot i starts a trace buffer (CONTROL TIME, then CONTROL BUFFER whose last data
    word is the buffer's slot count, ending with CONTROL BUFFER_END and _TRACECOOKIE),
    return that count; else None. (Layout seen in QNX 8.0 tracelogger files; traceprinter
    rejects files whose buffers do not follow it.)"""
    if i + 1 >= nslots:
        return None
    h0 = slot.unpack_from(data, start + 16 * i)[0]
    h1, _, _, n = slot.unpack_from(data, start + 16 * (i + 1))
    if (h0 & 0x7C00) >> 10 != C_CONTROL or h0 & 0x3FF != 1:
        return None
    if (h1 & 0x7C00) >> 10 != C_CONTROL or h1 & 0x3FF != 2 or n < 3 or i + n > nslots:
        return None
    he, _, c1, c2 = slot.unpack_from(data, start + 16 * (i + n - 1))
    if (he & 0x7C00) >> 10 != C_CONTROL or he & 0x3FF != 4 or (c1, c2) != (COOKIE, COOKIE):
        return None
    return n


def _cstr(words: list[int], endian: str) -> str:
    raw = struct.pack(f"{endian}{len(words)}I", *words)
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _s32(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


def _s64(v: int) -> int:
    return v - (1 << 64) if v & (1 << 63) else v


def _layout(entry: dict | None, nwords: int, nslots: int) -> tuple[str | None, list]:
    """Pick the documented layout that matches what was logged."""
    if not entry:
        return None, []
    for mode in (("fast", "wide") if nslots == 1 else ("wide", "fast")):
        spec = entry.get(mode)
        if not spec:
            continue
        need = sum(2 if b == 64 else 1 for _, b, _ in spec)
        # A combine event fills whole slots, so a wide event may carry one pad word.
        if need == nwords or (nslots > 1 and need + 1 == nwords) or (nslots == 1 and need <= 2 and nwords == 2):
            return mode, spec
    return None, []


def _kercall_fields(spec: list, words: list[int]) -> dict:
    out, i = {}, 0
    for name, bits, fmt in spec:
        if bits == 64:
            v = words[i] | (words[i + 1] << 32)          # low word first (checked on x86_64)
            i += 2
            out[name] = (str(_s64(v)) if fmt == "E" else f"0x{v:016x}")
        else:
            v = words[i]
            i += 1
            out[name] = (str(_s32(v)) if fmt == "D" else f"0x{v:08x}")
    return out


def _decode_event(h: int, words: list[int], nslots: int, endian: str) -> tuple[str, str, dict, bool, str | None]:
    """-> (class label, event label, fields, decoded per the docs?, 'fast'/'wide'/None)."""
    c = (h >> 10) & 0x1F
    ev = h & 0x3FF
    if c == C_PR_TH:
        if ev in PROCESS_EVENTS:
            name, fields = PROCESS_EVENTS[ev]
            f = {}
            for i, k in enumerate(fields):
                if k == "name":
                    f[k] = _cstr(words[i:], endian)
                    break
                if i < len(words):
                    f[k] = str(_s32(words[i]))
            return "PROCESS", name, f, len(f) == len(fields), "wide"
        if ev < 64 and ev in STATES:
            n = min(len(words), 4 if nslots > 1 else 2)
            f = {k: str(_s32(words[i])) for i, k in enumerate(THREAD_FIELDS[:n])}
            return "THREAD", "TH" + STATES[ev], f, True, "wide" if n == 4 else "fast"
        return "THREAD", f"TH?{ev}", {}, False, None
    if c == C_CONTROL:
        name = CONTROL_EVENTS.get(ev, f"CONTROL?{ev}")
        if ev == 1 and len(words) >= 2:
            return "CONTROL", name, {"msb": f"0x{words[0]:08x}", "lsb(offset)": f"0x{words[1]:08x}"}, True, "fast"
        if ev == 2 and len(words) >= 2:
            return "CONTROL", name, {"sequence": str(words[0]), "num_events": str(words[1])}, True, "fast"
        if ev == 8 and words:
            return "CONTROL", name, {"dropped_events": str(words[0])}, True, "fast"
        return "CONTROL", name, {}, ev == 4, "fast"
    if c == C_KERCALL:
        num, is_exit, is64 = ev & 0x7F, bool(ev & 0x80), bool(ev & KERCALL64)
        nm = kevspec.KERCALL_NAMES.get(num, f"KER?{num}")
        label = f"{nm}/{num:02d}"
        entry = kevspec.KERCALL_LAYOUTS.get((num, is_exit, is64))
        if entry is None and is64:
            # Some calls are logged with the 64-bit flag although the table documents
            # only one layout; use it only when its size matches exactly.
            entry = kevspec.KERCALL_LAYOUTS.get((num, is_exit, False))
        if (num, is_exit) in DISPUTED:
            entry = None
        mode, spec = _layout(entry, len(words), nslots)
        f = _kercall_fields(spec, words) if spec else {}
        return ("KER_EXIT" if is_exit else "KER_CALL"), label, f, bool(spec), mode
    if c == C_COMM:
        name, fields = COMM_EVENTS.get(ev, (f"COMM?{ev}", ()))
        if not fields or len(words) < 2:
            return "COMM", name, {}, False, None
        f = {}
        for i, k in enumerate(fields[:len(words)]):
            f[k] = f"0x{words[i]:08x}" if k in COMM_HEX else str(_s32(words[i]))
        # SIGNAL's wide form has more fields than are decoded here.
        return "COMM", name, f, ev != 9, "fast" if nslots == 1 else "wide"
    if c == C_INT:
        if ev == 7 and words:                  # _TRACE_INT_DELIVER
            return "INTERRUPT", "INT_DELIVER", {"intr_id": f"0x{words[0]:08x}"}, True, "fast"
        return "INTERRUPT", f"INT?{ev}", {}, False, None
    if c == C_SYSTEM:
        base = SYSTEM_EVENTS.get(ev & 0x1FF, f"SYS?{ev & 0x1FF}")
        return "SYSTEM", base + ("_64" if ev & KERCALL64 else ""), {}, False, None
    if c == C_USER:
        return "USER", f"EVENT{ev}", {}, False, None
    return CLASS_LABELS.get(c, f"CLASS{c}"), f"EVENT{ev}", {}, False, None


def decode(data: bytes, keep_words: bool = True) -> KevFile:
    """Decode a whole .kev file. Events come back sorted by 64-bit time (stable on
    file order), each a dict:
        t       full 64-bit cycle count     cpu, cls, ev (traceprinter labels)
        f       {field: str}  (traceprinter names; empty if not decoded)
        decoded True if the fields follow the documented layout
        mode    'fast' / 'wide' / None      off, slots: file offset and slot count
        words   raw data words after the timestamp (provenance)
    """
    header, pos = parse_header(data)
    little = header.get("LITTLE_ENDIAN", "").strip().upper() == "TRUE"
    big = header.get("BIG_ENDIAN", "").strip().upper() == "TRUE"
    if not little and not big:
        raise KevError("the .kev header gives no byte order (TRACE_LITTLE_ENDIAN/BIG_ENDIAN)")
    endian = "<" if little else ">"
    enc = header.get("ENCODING", "16 byte events").strip()
    if not enc.startswith("16 byte"):
        raise KevError(f"unsupported event encoding {enc!r} (only 16-byte events are documented)")
    try:
        cps = int(header.get("CYCLES_PER_SEC", "0").strip() or 0)
    except ValueError:
        cps = 0
    if cps <= 0:
        raise KevError("the .kev header has no TRACE_CYCLES_PER_SEC")
    try:
        ncpu = int(header.get("CPU_NUM", "0").strip() or 0)
        syspage = int(header.get("SYSPAGE_LEN", "0").strip() or 0)
    except ValueError as e:
        raise KevError(f"bad .kev header value: {e}") from None
    start = pos + syspage
    if start > len(data):
        raise KevError("truncated .kev file: it ends inside the system page")
    body = len(data) - start
    nslots, tail = divmod(body, SLOT)

    stats: Counter = Counter()
    problems: list = []

    def problem(off: int, text: str) -> None:
        stats["problems"] += 1
        if len(problems) < 50:
            problems.append((off, text))

    if tail:
        problem(start + nslots * SLOT, f"{tail} trailing bytes (a partial event slot): file truncated?")
        stats["truncated_tail_bytes"] = tail

    slot = struct.Struct(f"{endian}4I")
    unpack = slot.unpack_from

    # Buffer framing: a check on the file's integrity (decoding below goes slot by slot
    # and does not depend on it).
    i = 0
    while i < nslots:
        n = buffer_slots(data, start, i, nslots, slot)
        if n is None:
            problem(start + i * SLOT, "trace buffer framing broken here (no CONTROL TIME/BUFFER "
                                      "start, or no BUFFER_END where the count says): the rest "
                                      "of the file is decoded slot by slot")
            stats["framing_errors"] += 1
            break
        stats["buffers"] += 1
        i += n

    msb: dict = {}
    last: dict = {}
    default_msb = None
    pending: dict = {}                  # (cpu, header w/o struct bits, lsb) -> [t, off, slots, words]
    out = []

    def stamp(cpu: int, lsb: int) -> int:
        m = msb.get(cpu, default_msb if default_msb is not None else 0)
        prev = last.get(cpu)
        if prev is None:
            last[cpu] = lsb
        elif lsb < prev and prev - lsb > 0x80000000:      # the low 32 bits rolled over
            m += 1
            msb[cpu] = m
            last[cpu] = lsb
        elif lsb > prev and lsb - prev > 0x80000000:      # interlaced from before a rollover
            return ((m - 1) << 32) | lsb
        elif lsb > prev:
            last[cpu] = lsb
        return (m << 32) | lsb

    def emit(h: int, t: int, off: int, slots: int, words: list[int]) -> None:
        cls, ev, f, ok, mode = _decode_event(h, words, slots, endian)
        cpu = (h >> 24) & 0x3F
        if ncpu and cpu >= ncpu:
            problem(off, f"event on CPU {cpu} but the header says {ncpu} CPUs")
        if not ok:
            stats["not_decoded"] += 1
        e = {"t": t, "cpu": cpu, "cls": cls, "ev": ev, "f": f, "decoded": ok, "mode": mode,
             "off": off, "slots": slots}
        if h & INJECTED:
            e["injected"] = True
        if keep_words:
            e["words"] = tuple(words)
        out.append(e)

    for i in range(nslots):
        off = start + i * SLOT
        h, d0, d1, d2 = unpack(data, off)
        st = h >> 30
        c = (h >> 10) & 0x1F
        cpu = (h >> 24) & 0x3F
        stats["slots"] += 1
        if c == C_EMPTY and (h & 0x3FF) == 0:
            stats["empty_slots"] += 1
            continue
        if c == C_CONTROL and (h & 0x3FF) == 1 and st == S_SIMPLE:      # CONTROL TIME
            msb[cpu] = d1
            last[cpu] = d0
            if default_msb is None:
                default_msb = d1
        if c == C_CONTROL and (h & 0x3FF) == 4 and (d1, d2) != (COOKIE, COOKIE):
            problem(off, "CONTROL BUFFER_END without _TRACECOOKIE")
        if c == C_CONTROL and (h & 0x3FF) == 8:
            stats["dropped_events"] += d1
        key = (cpu, h & 0x3FFFFFFF, d0)
        if st == S_SIMPLE:
            emit(h, stamp(cpu, d0), off, 1, [d1, d2])
        elif st == S_BEGIN:
            if key in pending:
                problem(pending[key][1], "combine event begun again before it ended (dropped)")
                stats["incomplete_combine"] += 1
            pending[key] = [stamp(cpu, d0), off, 1, [d1, d2]]
        else:
            p = pending.get(key)
            if p is None:
                problem(off, "combine event continuation without a beginning (orphan slot)")
                stats["orphan_slots"] += 1
                continue
            p[2] += 1
            p[3] += [d1, d2]
            if st == S_END:
                del pending[key]
                emit(h, p[0], p[1], p[2], p[3])
    for key, p in pending.items():
        problem(p[1], "combine event without an end (truncated or lost)")
        stats["incomplete_combine"] += 1
    out.sort(key=lambda e: (e["t"], e["off"]))
    stats["events"] = len(out)
    by: Counter = Counter(f"{e['cls']}:{e['ev'].split('/')[0]}" for e in out)
    s = dict(stats)
    s["by_type"] = dict(by.most_common())
    s["decoded_events"] = len(out) - stats["not_decoded"]
    return KevFile(header=header, little_endian=little, cycles_per_sec=cps, cpus=ncpu,
                   events=out, stats=s, problems=problems)


def is_kev(data: bytes) -> bool:
    return data.startswith(MAGIC)
