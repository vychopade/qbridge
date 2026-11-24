"""Check qbridge's native .kev decoder against the QNX SDP's traceprinter.

For each .kev file, both decoders' outputs are compared event by event:
an event is identified by (low 32 bits of the timestamp, CPU, class, event),
which is what traceprinter prints on each line, and events with the same
identity are paired in order. For each pair, every field that both sides
print is compared by value (hex and decimal, signed and unsigned forms of
the same 32-bit word are equal). The report counts, per event type:
events traceprinter printed, events paired, events whose fields qbridge
decodes, fields compared and fields equal, and lists every mismatch (first 20
per file). Events only one side has are reported as missing / extra.

Ordering is checked per CPU: among paired events, how often qbridge's order
disagrees with traceprinter's. Full 64-bit timestamps are checked against an
independent reconstruction from traceprinter's text (CONTROL TIME msb plus
rollovers, parsers/kev.py).

This needs traceprinter's output, so it runs where the SDP is installed (or on
.txt files saved from it). It never needs a target.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from .parsers import kevbin

_LINE = re.compile(r"^t:0x([0-9a-f]+) CPU:(\d+) (\w+)\s+:(\S+)\s*(.*)$")
_KV = re.compile(r'([A-Za-z_][\w\[\]\->().]*?):(".*?" \((?:0x[0-9a-fA-F]+ ?)+\)|\S+)')
_EQ = re.compile(r"(\w+) = ([^,\s]+)")


def parse_text(text: str) -> list[dict]:
    """traceprinter output -> [{lsb, cpu, cls, ev, f: {name: raw str}, n: file order}]."""
    out, cur = [], None
    lines = iter(text.splitlines())
    for line in lines:
        # A message buffer printed as a string can contain newlines: join until it closes.
        while re.search(r':"', line) and not re.search(r'" \((?:0x[0-9a-fA-F]+ ?)+\)', line):
            nxt = next(lines, None)
            if nxt is None:
                break
            line += "\\n" + nxt
        m = _LINE.match(line)
        if m:
            cur = {"lsb": int(m[1], 16), "cpu": int(m[2]), "cls": m[3], "ev": m[4], "f": {},
                   "n": len(out)}
            out.append(cur)
            rest = m[5]
        elif cur is not None and line.startswith(" "):
            rest = line
            nm = re.match(r"^\s*name:(.*)$", line)
            if nm:                                # names may contain spaces
                cur["f"]["name"] = nm[1].strip()
                continue
        else:
            continue
        if " = " in rest:
            cur["f"].update(_EQ.findall(rest))
        else:
            for k, v in _KV.findall(rest):
                cur["f"][k.strip()] = v
    return out


def _ident(cls: str, ev: str) -> tuple[str, str]:
    """Kernel calls by number: traceprinter shortens some exit labels (SYNC_CONDVAR_SIG/83)."""
    if cls in ("KER_CALL", "KER_EXIT") and "/" in ev:
        return cls, ev.rsplit("/", 1)[1].lstrip("0") or "0"
    return cls, ev


def _num(v: str):
    v = v.strip()
    m = re.fullmatch(r"(-?)0x([0-9a-fA-F]+)(?:\(\d+\))?", v)
    if m:
        n = int(m[2], 16)
        return -n if m[1] else n
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return None


def _norm(n: int) -> int:
    if -(1 << 31) <= n < 0:
        return n + (1 << 32)
    if -(1 << 63) <= n < 0:
        return n + (1 << 64)
    return n


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    x, y = _num(a), _num(b)
    if x is not None and y is not None:
        return _norm(x) == _norm(y)
    return a.strip('"') == b.strip('"')


def _expand(f: dict) -> dict:
    """traceprinter prints msg[0]:"text" (0xA 0xB 0xC) for three words: split them."""
    out = {}
    for k, v in f.items():
        m = re.fullmatch(r'".*" \(((?:0x[0-9a-fA-F]+ ?)+)\)', v)
        im = re.fullmatch(r"(\w+)\[(\d+)\]", k)
        if m and im:
            for j, w in enumerate(m[1].split()):
                out[f"{im[1]}[{int(im[2]) + j}]"] = w
        else:
            out[k] = v
    return out


def _type(cls: str, ev: str) -> str:
    return f"{cls}:{ev.split('/')[0]}"


def compare(kev_bytes: bytes, text: str) -> dict:
    """Compare one trace. Returns the per-file section of the report."""
    try:
        kf = kevbin.decode(kev_bytes)
    except kevbin.KevError as e:
        return {"parse_failure": str(e)}
    tp = parse_text(text)
    mine = kf.events
    by_type: dict = defaultdict(Counter)
    groups: dict = defaultdict(list)
    for i, e in enumerate(mine):
        groups[(e["t"] & 0xFFFFFFFF, e["cpu"]) + _ident(e["cls"], e["ev"])].append(i)
    used = [False] * len(mine)
    cursor: dict = defaultdict(int)
    pairs = []                                 # (text index, my index)
    mismatches, missing = [], []
    fields_cmp = fields_eq = 0
    for t in tp:
        typ = _type(t["cls"], t["ev"])
        by_type[typ]["traceprinter"] += 1
        key = (t["lsb"], t["cpu"]) + _ident(t["cls"], t["ev"])
        lst = groups.get(key, [])
        k = cursor[key]
        if k >= len(lst):
            by_type[typ]["missing"] += 1
            if len(missing) < 20:
                missing.append({"line": t["n"], "t": f"0x{t['lsb']:08x}", "cpu": t["cpu"], "type": typ})
            continue
        cursor[key] = k + 1
        i = lst[k]
        used[i] = True
        pairs.append((t["n"], i))
        e = mine[i]
        by_type[typ]["matched"] += 1
        if e["ev"] != t["ev"]:
            by_type[typ]["label_differs"] += 1
        if not e["decoded"]:
            continue
        by_type[typ]["decoded"] += 1
        tf = _expand(t["f"])
        bad = []
        for name, val in e["f"].items():
            if name not in tf:
                continue
            fields_cmp += 1
            by_type[typ]["fields_compared"] += 1
            if _same(val, tf[name]):
                fields_eq += 1
                by_type[typ]["fields_equal"] += 1
            else:
                bad.append({"field": name, "qbridge": val, "traceprinter": tf[name]})
        if bad:
            by_type[typ]["field_mismatch_events"] += 1
            if len(mismatches) < 20:
                mismatches.append({"line": t["n"], "t": f"0x{t['lsb']:08x}", "cpu": t["cpu"],
                                   "type": typ, "fields": bad})
        else:
            by_type[typ]["fully_equal"] += 1
    extra = [i for i, u in enumerate(used) if not u]
    for i in extra:
        by_type[_type(mine[i]["cls"], mine[i]["ev"])]["extra"] += 1

    # Ordering per CPU: walk paired events in traceprinter's order and count the
    # places where qbridge's order goes backwards on the same CPU.
    out_of_order = 0
    prev: dict = {}
    for _, i in sorted(pairs):
        c = mine[i]["cpu"]
        if c in prev and i < prev[c]:
            out_of_order += 1
        prev[c] = i

    # 64-bit timestamps against the independent text reconstruction.
    ts_checked = ts_equal = 0
    try:
        recon = _text_times(text)
        for n, i in pairs:
            if n in recon:
                ts_checked += 1
                ts_equal += recon[n] == mine[i]["t"]
    except ValueError:
        pass

    matched = len(pairs)
    return {
        "header": {k: kf.header.get(k) for k in ("SYS_RELEASE", "KERNEL_VERSION", "MACHINE",
                                                  "CPU_NUM", "CYCLES_PER_SEC", "TRACELOGGER_ARGS")},
        "events_traceprinter": len(tp),
        "events_qbridge": len(mine),
        "matched": matched,
        "missing_in_qbridge": len(tp) - matched,
        "extra_in_qbridge": len(extra),
        "decoded_by_qbridge": sum(c["decoded"] for c in by_type.values()),
        "decoded_fully_equal": sum(c["fully_equal"] for c in by_type.values()),
        "field_mismatch_events": sum(c["field_mismatch_events"] for c in by_type.values()),
        "fields_compared": fields_cmp,
        "fields_equal": fields_eq,
        "order_disagreements": out_of_order,
        "timestamps_checked": ts_checked,
        "timestamps_equal": ts_equal,
        "decoder_problems": kf.stats.get("problems", 0),
        "decoder_problem_samples": [{"offset": o, "text": t} for o, t in kf.problems[:10]],
        "dropped_events_reported_by_kernel": kf.stats.get("dropped_events", 0),
        "by_type": {k: dict(v) for k, v in sorted(by_type.items())},
        "mismatch_samples": mismatches,
        "missing_samples": missing,
        "extra_samples": [{"t": f"0x{mine[i]['t'] & 0xFFFFFFFF:08x}", "cpu": mine[i]["cpu"],
                           "type": _type(mine[i]["cls"], mine[i]["ev"])} for i in extra[:20]],
    }


def _text_times(text: str) -> dict:
    """traceprinter line index -> 64-bit time, rebuilt from the text alone (kev.py's rule)."""
    out, msb, last = {}, defaultdict(int), {}
    for n, t in enumerate(parse_text(text)):
        cpu, lsb = t["cpu"], t["lsb"]
        if t["cls"] == "CONTROL" and t["ev"] == "TIME" and "msb" in t["f"]:
            msb[cpu] = int(t["f"]["msb"], 16)
            last[cpu] = lsb
        elif cpu in last and lsb < last[cpu] and last[cpu] - lsb > 0x80000000:
            msb[cpu] += 1
            last[cpu] = lsb
        elif cpu in last and lsb > last[cpu] and lsb - last[cpu] > 0x80000000:
            out[n] = ((msb[cpu] - 1) << 32) | lsb
            continue
        elif cpu not in last or lsb > last[cpu]:
            last[cpu] = lsb
        out[n] = (msb[cpu] << 32) | lsb
    return out


def _stem(p: Path) -> str:
    n = p.name
    for suf in (".gz", ".kev"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n


def _readz(p: Path) -> bytes:
    data = p.read_bytes()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def traceprinter_text(kev_path: Path, text_path: Path | None = None) -> str:
    """traceprinter's output for a .kev: a saved copy beside it (<stem>.txt,
    <stem>.traceprinter.txt, either gzipped), or a fresh run of the SDP's traceprinter."""
    stem = _stem(kev_path)
    cands = [text_path] if text_path else [kev_path.with_name(stem + s) for s in
                                           (".txt", ".txt.gz", ".traceprinter.txt",
                                            ".traceprinter.txt.gz")]
    for cand in cands:
        if cand and cand.is_file():
            return _readz(cand).decode("utf-8", errors="replace").replace("\r\n", "\n")
    from . import tools                           # needs the SDP
    exe = tools._traceprinter()
    out = kev_path.with_name(stem + ".traceprinter.txt")
    src = kev_path
    if kev_path.read_bytes()[:2] == b"\x1f\x8b":
        src = kev_path.with_name(stem + ".unzipped.kev")
        src.write_bytes(_readz(kev_path))
    r = subprocess.run([exe, "-f", str(src), "-o", str(out)], capture_output=True, text=True)
    if r.returncode != 0 or not out.is_file():
        raise RuntimeError(f"traceprinter failed on {kev_path}: {(r.stderr or r.stdout)[-300:]}")
    return out.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")


def _label(p: Path) -> str:
    """A file's name in the report: with its folder when the name alone is generic."""
    return f"{p.parent.name}/{p.name}" if _stem(p) in ("trace", "tracebuffer") else p.name


def _tp_version(text: str) -> str | None:
    m = re.search(r"TRACEPRINTER version ([\w.]+)", text[:400])
    return m[1] if m else None


def validate(paths: list[Path]) -> dict:
    from . import __version__
    files, totals, types = [], Counter(), defaultdict(Counter)
    versions = set()
    for p in paths:
        data = _readz(p)
        try:
            text = traceprinter_text(p)
        except Exception as e:                        # noqa: BLE001 - reported, not raised
            files.append({"file": _label(p), "sha256": hashlib.sha256(data).hexdigest(),
                          "traceprinter_failure": str(e)})
            totals["traceprinter_failures"] += 1
            continue
        if _tp_version(text):
            versions.add(_tp_version(text))
        r = compare(data, text)
        r = {"file": _label(p), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
             "traceprinter_version": _tp_version(text), **r}
        files.append(r)
        if "parse_failure" in r:
            totals["parse_failures"] += 1
            continue
        for k in ("events_traceprinter", "events_qbridge", "matched", "missing_in_qbridge",
                  "extra_in_qbridge", "decoded_by_qbridge", "decoded_fully_equal",
                  "field_mismatch_events", "fields_compared", "fields_equal",
                  "order_disagreements", "timestamps_checked", "timestamps_equal",
                  "decoder_problems", "dropped_events_reported_by_kernel"):
            totals[k] += r[k]
        for t, c in r["by_type"].items():
            types[t].update(c)
    totals["files"] = len(paths)
    tot = dict(totals)
    tot["not_decoded_matched"] = tot.get("matched", 0) - tot.get("decoded_by_qbridge", 0)
    by_type = {k: dict(v) for k, v in sorted(types.items(), key=lambda kv: -kv[1]["traceprinter"])}
    summary = {
        "events_examined": tot.get("events_traceprinter", 0),
        "supported_events": tot.get("decoded_by_qbridge", 0),
        "supported_events_fully_matched": tot.get("decoded_fully_equal", 0),
        "unsupported_events_matched_on_identity_time_cpu": tot["not_decoded_matched"],
        "mismatches": (tot.get("missing_in_qbridge", 0) + tot.get("extra_in_qbridge", 0)
                       + tot.get("field_mismatch_events", 0)),
        "parsing_failures": tot.get("parse_failures", 0) + tot.get("traceprinter_failures", 0),
        "event_types": len(by_type),
        "event_types_with_fields_decoded": sum(1 for c in by_type.values() if c.get("decoded")),
        "event_types_name_time_cpu_only": [k for k, c in by_type.items() if not c.get("decoded")],
        "event_types_partly_decoded": {k: f"{c['decoded']} of {c['traceprinter']}"
                                       for k, c in by_type.items()
                                       if 0 < c.get("decoded", 0) < c["traceprinter"]},
        "largest_event_types": {k: round(c["traceprinter"] / max(1, tot.get("events_traceprinter", 0)), 4)
                                for k, c in list(by_type.items())[:5]},
    }
    return {
        "report": "qbridge native .kev decoder vs QNX traceprinter",
        "generated": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "qbridge_version": __version__,
        "traceprinter_versions": sorted(versions),
        "method": ("Events paired by (timestamp low 32 bits, CPU, class, event); fields compared "
                   "by value where both decoders print the same field name. 'supported' means "
                   "qbridge decodes that event's fields from the documented layout; other events "
                   "are matched on identity, time and CPU only. 64-bit timestamps are checked "
                   "against a reconstruction from traceprinter's text (CONTROL TIME msb plus "
                   "rollovers); order per CPU against traceprinter's."),
        "summary": summary,
        "totals": tot,
        "by_type": by_type,
        "files": files,
    }
