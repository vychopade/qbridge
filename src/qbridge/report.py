"""Self-contained HTML report for a measured fix experiment.

Built only from captured data: the experiment record, each run's saved report
and samples.csv, an optional saved diagnosis, and saved timing checks. No
number in the report is typed by hand; every one comes from those files.
The page has no external dependencies (inline CSS and SVG).
"""

from __future__ import annotations

import html
import time
from typing import Callable

from . import __version__, deadlines

PARTS_LABEL = {
    "backlog": "Backlog (previous cycle overran)",
    "latency": "Scheduling latency",
    "lock_wait": "Lock wait",
    "exec": "CPU time (own work)",
    "preempted": "Preempted while ready",
}
CAUSE_LABEL = {
    "lock_wait": "long lock wait", "latency": "late wake-up", "preempted": "preempted",
    "exec": "more CPU time than usual", "backlog": "previous cycle overran",
}

e_ = html.escape


def _ms(us: float | None, digits: int = 1) -> str:
    return "–" if us is None else f"{us / 1000:.{digits}f} ms"


def _range(vals: list[float], fmt: Callable[[float], str]) -> str:
    lo, hi = fmt(min(vals)), fmt(max(vals))
    if lo == hi:                       # equal once rounded
        return lo
    if lo.endswith(" ms") and hi.endswith(" ms"):
        return f"{lo[:-3]}–{hi}"       # unit once: "37.0–37.1 ms"
    return f"{lo}–{hi}"


def _short_run(run_id: str, exp_id: str) -> str:
    """Run ids repeat the experiment id; show the distinctive part in tables."""
    return run_id.replace(exp_id + "-", "")


# -- charts -------------------------------------------------------------------

def _series_chart(samples: list[deadlines.Sample], deadline_us: float, y_max_ms: float,
                  css_class: str, title: str) -> str:
    """Response time per cycle; misses drawn in the alert colour."""
    w, h, left, bottom, top, right = 880, 210, 52, 30, 14, 12
    pw, ph = w - left - right, h - top - bottom
    if not samples:
        return ""
    t_max = max(s.release for s in samples) or 1.0
    step = max(1, len(samples) // 2000)

    def x(t):
        return left + pw * t / t_max

    def y(ms):
        return top + ph * (1 - min(ms, y_max_ms) / y_max_ms)

    pts_ok, pts_miss = [], []
    for s in samples[::step]:
        ms = s.response / 1000
        (pts_miss if s.response > deadline_us else pts_ok).append(f"{x(s.release):.1f},{y(ms):.1f}")
    ticks = []
    for v in _nice_ticks(y_max_ms):
        ticks.append(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                     f'<text class="tick" x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
    secs = int(t_max / 1e6)
    xt = "".join(f'<text class="tick" x="{x(sv * 1e6):.1f}" y="{h - 10}" text-anchor="middle">{sv}s</text>'
                 for sv in range(0, secs + 1, max(1, secs // 10 or 1)))
    dl = y(deadline_us / 1000)
    dots_ok = "".join(f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="1.6"/>' for p in pts_ok)
    dots_miss = "".join(f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="2"/>' for p in pts_miss)
    return (f'<figure class="chart"><figcaption>{e_(title)}</figcaption>'
            f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{e_(title)}">'
            + "".join(ticks)
            + f'<line class="deadline" x1="{left}" x2="{w - right}" y1="{dl:.1f}" y2="{dl:.1f}"/>'
            f'<text class="deadline-label" x="{w - right - 4}" y="{dl - 5:.1f}" text-anchor="end">'
            f'deadline {deadline_us / 1000:g} ms</text>'
            f'<g class="{css_class}">{dots_ok}</g><g class="miss">{dots_miss}</g>'
            + xt
            + f'<text class="axis" x="14" y="{top + ph / 2:.0f}" transform="rotate(-90 14 {top + ph / 2:.0f})" '
              f'text-anchor="middle">response (ms)</text></svg></figure>')


def _cdf_chart(before: list[float], after: list[float], deadline_us: float) -> str:
    """Share of cycles that finished within x ms, before and after."""
    w, h, left, bottom, top, right = 880, 230, 52, 34, 14, 12
    pw, ph = w - left - right, h - top - bottom
    x_max = max(max(before or [0]), max(after or [0]), deadline_us) / 1000 * 1.05

    def x(ms):
        return left + pw * ms / x_max

    def y(frac):
        return top + ph * (1 - frac)

    def path(vals):
        v = sorted(vals)
        n = len(v)
        if not n:
            return ""
        step = max(1, n // 1500)
        pts = [f"{x(v[i] / 1000):.1f},{y((i + 1) / n):.1f}" for i in range(0, n, step)]
        pts.append(f"{x(v[-1] / 1000):.1f},{y(1):.1f}")
        return "M" + " L".join(pts)

    grid = "".join(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y(f):.1f}" y2="{y(f):.1f}"/>'
                   f'<text class="tick" x="{left - 6}" y="{y(f) + 4:.1f}" text-anchor="end">{int(f * 100)}%</text>'
                   for f in (0, 0.25, 0.5, 0.75, 1.0))
    xt = "".join(f'<text class="tick" x="{x(v):.1f}" y="{h - 14}" text-anchor="middle">{v:g}</text>'
                 for v in _nice_ticks(x_max))
    dl = x(deadline_us / 1000)
    return (f'<figure class="chart"><figcaption>Share of cycles finished within a given time '
            f'(all runs). Left and steep is good; everything right of the deadline is a miss.</figcaption>'
            f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="response time distribution">'
            + grid
            + f'<line class="deadline" x1="{dl:.1f}" x2="{dl:.1f}" y1="{top}" y2="{top + ph}"/>'
              f'<path class="line before" d="{path(before)}"/><path class="line after" d="{path(after)}"/>'
            + xt
            + f'<text class="axis" x="{left + pw / 2:.0f}" y="{h - 1}" text-anchor="middle">response (ms)</text>'
              '</svg><div class="legend"><span class="key before"></span>before'
              '<span class="key after"></span>after<span class="key deadline"></span>deadline</div></figure>')


_STATE_CLASS = {"RUNNING": "st-run", "READY": "st-ready", "MUTEX": "st-mutex"}


def _trace_timeline(tl: dict) -> str:
    """One lane per thread across the window around the longest wait."""
    lanes = tl.get("lanes") or []
    if not lanes:
        return ""
    w, left, right, top, lane_h, gap = 760, 150, 12, 10, 22, 10
    plot_w = w - left - right
    h = top + len(lanes) * (lane_h + gap) + 34
    span = max(tl["window_ms"], 0.001)
    x = lambda ms: left + plot_w * ms / span
    axis_y = top + len(lanes) * (lane_h + gap) + 4
    out = []
    for t in _nice_ticks(span):           # grid first, so the bars sit on top
        if t <= span:
            out.append(f'<line class="grid" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top}" y2="{axis_y}"/>'
                       f'<text class="tick" x="{x(t):.1f}" y="{axis_y + 14}" text-anchor="middle">'
                       f'{t:g}</text>')
    for i, ln in enumerate(lanes):
        y = top + i * (lane_h + gap)
        out.append(f'<text class="lane" x="{left - 8}" y="{y + lane_h / 2 + 4}" text-anchor="end">'
                   f'{e_(ln["lane"])}: {e_(ln["thread"].split(" ", 1)[-1].strip(chr(39)))}</text>')
        out.append(f'<rect class="st-bg" x="{left}" y="{y + lane_h / 2 - 1}" width="{plot_w}" height="2"/>')
        for sp in ln["spans"]:
            cls = _STATE_CLASS.get(sp["state"])
            if not cls:
                continue                  # sleeping / other states: just the thin baseline
            x0, x1 = x(sp["start"]), x(sp["end"])
            out.append(f'<rect class="{cls}" x="{x0:.1f}" y="{y}" width="{max(x1 - x0, 0.8):.1f}" '
                       f'height="{lane_h}"><title>{e_(sp["state"])} at priority {sp["priority"]}, '
                       f'{sp["end"] - sp["start"]:.2f} ms, CPU {sp["cpu"]}</title></rect>')
    out.append(f'<text class="axis" x="{left + plot_w / 2}" y="{h - 2}" text-anchor="middle">'
               f'ms from {tl["window_start_ms"]:.1f} ms into the trace</text>')
    return (f'<figure class="chart"><figcaption>Kernel trace around the longest wait</figcaption>'
            f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="thread states from the kernel trace">'
            + "".join(out) + '</svg><div class="legend"><span class="key st-run"></span>running'
            '<span class="key st-ready"></span>ready (waiting for the CPU)'
            '<span class="key st-mutex"></span>blocked on a mutex'
            '<span class="key st-bg"></span>sleeping or other</div></figure>')


def _trace_block(f: dict) -> str:
    t = f.get("trace")
    if not t:
        return ""
    ok = t.get("status") == "confirmed"
    steps = "".join(
        f'<li class="{"ok" if st["supported"] else "no"}"><strong>'
        f'{"Seen" if st["supported"] else "Not shown"}:</strong> {e_(st["claim"])}. '
        f'<span class="muted">{e_(st["detail"])}</span></li>' for st in t.get("steps", []))
    limits = "".join(f"<li>{e_(x)}</li>" for x in t.get("limits", []))
    head = ("Confirmed by kernel trace: every step was seen." if ok else
            "Not confirmed by the kernel trace; the confidence above is unchanged. Steps not shown: "
            + "; ".join(t.get("not_shown", [])) + ".")
    return (f'<h4>Kernel trace check</h4><p class="{"trace-ok" if ok else "trace-no"}">{e_(head)}</p>'
            f'<ul class="steps">{steps}</ul>{_trace_timeline(t.get("timeline") or {})}'
            f'<ul class="muted">{limits}</ul>'
            f'<p class="note">Trace <code>{e_(t.get("trace_id") or "")}</code>, '
            f'{t.get("trace_ms", "?")} ms, recorded with tracelogger in the same run as the '
            f'snapshots.</p>')


def _nice_ticks(vmax: float) -> list[float]:
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500):
        if vmax / step <= 8:
            return [i * step for i in range(int(vmax // step) + 1)]
    return [0, vmax]


# -- report ---------------------------------------------------------------------

def build(experiment: dict, load_run: Callable[[str], dict], diagnosis: dict | None = None,
          timing_checks: list[dict] | None = None, generated: str | None = None) -> str:
    """Render the report. `load_run(run_id)` returns {"report": ..., "samples": csv text or None,
    "summary": the program's own summary line or None}."""
    e = experiment
    generated = generated or time.strftime("%Y-%m-%d %H:%M")
    base = [load_run(r["run_id"]) for r in e["baseline"]["runs"]]
    cand = [load_run(r["run_id"]) for r in (e.get("candidate") or {}).get("runs", [])]
    breps = [b["report"] for b in base]
    creps = [c["report"] for c in cand]
    cmp = e.get("comparison") or (deadlines.compare_sets(breps, creps) if creps else None)
    deadline_us = breps[0]["deadline_us"]
    task = breps[0].get("task", e["program"])
    p = e.get("proposal") or {}
    verdict = (cmp or {}).get("verdict", "not measured")

    def samples_of(runs_):
        out = []
        for r in runs_:
            if r.get("samples"):
                out.append(deadlines.parse_samples_csv(r["samples"]))
        return out

    bs, cs = samples_of(base), samples_of(cand)
    all_resp = [s.response for ss in bs + cs for s in ss]
    y_max = max(max(all_resp or [deadline_us]), deadline_us) / 1000 * 1.08

    parts = []
    parts.append(f"""
<header>
  <p class="eyebrow">qbridge RT · measured fix experiment</p>
  <h1>Why <code>{e_(task)}</code> missed its {deadline_us / 1000:g} ms deadline, and whether the fix worked</h1>
  <p class="meta">Program <code>{e_(e['program'])}</code> · experiment <code>{e_(e['id'])}</code> ·
  generated {e_(generated)} · qbridge {e_(__version__)}</p>
</header>""")

    # Verdict
    if cmp:
        kp = [("Missed deadlines per run", _range(cmp["misses"]["before"]["runs"], lambda v: f"{v:.0f}"),
               _range(cmp["misses"]["after"]["runs"], lambda v: f"{v:.0f}"),
               f"of {breps[0]['cycles']} cycles"),
              ("Worst response", _range(cmp["worst_response_us"]["before"]["runs"], _ms),
               _range(cmp["worst_response_us"]["after"]["runs"], _ms), ""),
              ("99th percentile", _range(cmp["p99_response_us"]["before"]["runs"], _ms),
               _range(cmp["p99_response_us"]["after"]["runs"], _ms), ""),
              ("Worst lock wait", _range(cmp["worst_lock_wait_us"]["before"]["runs"], _ms),
               _range(cmp["worst_lock_wait_us"]["after"]["runs"], _ms), "")]
        kpis = "".join(f'<div class="kpi"><div class="kpi-label">{e_(l)}</div>'
                       f'<div class="kpi-values"><span class="before-v">{e_(b)}</span>'
                       f'<span class="arrow">→</span><span class="after-v">{e_(a)}</span></div>'
                       f'<div class="kpi-note">{e_(n)}</div></div>' for l, b, a, n in kp)
        parts.append(f"""
<section class="verdict v-{e_(verdict.replace(' ', '-'))}">
  <div class="pill">{e_(verdict)}</div>
  <p class="lead">{e_(cmp['explanation'])}</p>
  <div class="kpis">{kpis}</div>
</section>""")
    else:
        parts.append('<section class="verdict"><div class="pill">not measured</div>'
                     '<p class="lead">The change has not been measured yet.</p></section>')

    # Runs table
    def row(label, r, rid):
        rr = r["response_us"]
        pr = r["parts_us"]
        return (f"<tr><td>{e_(label)}</td><td class='num'>{r['misses']}</td>"
                f"<td class='num'>{_ms(rr['max'])}</td><td class='num'>{_ms(rr['p99'])}</td>"
                f"<td class='num'>{_ms(rr['mean'], 2)}</td><td class='num'>{_ms(pr['lock_wait']['max'])}</td>"
                f"<td class='num'>{_ms(pr['latency']['max'])}</td>"
                f"<td><code class='small' title='{e_(rid)}'>{e_(_short_run(rid, e['id']))}</code></td></tr>")

    rows = "".join(row(f"Before {i + 1}", r, e["baseline"]["runs"][i]["run_id"]) for i, r in enumerate(breps))
    rows += "".join(row(f"After {i + 1}", r, e["candidate"]["runs"][i]["run_id"]) for i, r in enumerate(creps))
    w = e["workload"]
    cpus = sorted({(r.get("summary") or {}).get("cpu") for r in base + cand} - {None})
    cpu_text = (f" All threads ran on CPU {', '.join(str(c) for c in cpus)} (see the timing check in "
                "Limitations)." if cpus else "")
    before_args = " ".join(w.get("args") or []) or "(none)"
    after_args = " ".join((e.get("candidate") or {}).get("args", w.get("args")) or []) or "(none)"
    if e.get("candidate") and before_args != after_args:
        cpu_text += (f" Program arguments before: {before_args}; after: {after_args}. "
                     "The binary was the same on both sides, so only the arguments differ.")
    elif w.get("args"):
        cpu_text += f" Program arguments: {before_args}."
    parts.append(f"""
<section>
  <h2>Measurements</h2>
  <p>Each run: <code>{e_(e['program'])}</code> for {w['seconds']} s
  ({breps[0]['cycles']} cycles of {(breps[0].get('period_us') or 0) / 1000:g} ms), the same workload before and after.
  Statistics are computed by qbridge from the per-cycle samples.{e_(cpu_text)}</p>
  <div class="table-wrap"><table>
    <thead><tr><th>Run</th><th>Misses</th><th>Worst</th><th>p99</th><th>Mean</th>
    <th>Worst lock wait</th><th>Worst sched. latency</th><th>Saved run</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
</section>""")

    # Charts
    charts = []
    if bs:
        charts.append(_series_chart(bs[0], deadline_us, y_max, "pts before",
                                    "Before the change: response time of every cycle (run 1)"))
    if cs:
        charts.append(_series_chart(cs[0], deadline_us, y_max, "pts after",
                                    "After the change: response time of every cycle (run 1)"))
    if bs and cs:
        charts.append(_cdf_chart([s.response for ss in bs for s in ss],
                                 [s.response for ss in cs for s in ss], deadline_us))
    if charts:
        parts.append(f"<section><h2>Response time</h2>{''.join(charts)}"
                     "<p class='note'>Dots above the dashed line are missed deadlines.</p></section>")

    # Where the time went
    prow = "".join(
        f"<tr><td>{e_(PARTS_LABEL[k])}</td>"
        f"<td class='num'>{_range([r['parts_us'][k]['max'] for r in breps], _ms)}</td>"
        + (f"<td class='num'>{_range([r['parts_us'][k]['max'] for r in creps], _ms)}</td>" if creps else "")
        + "</tr>" for k in PARTS_LABEL)
    roots = {}
    for r in breps:
        for c, n in r.get("chain_root_causes", {}).items():
            roots[c] = roots.get(c, 0) + n
    root_text = ", ".join(f"{CAUSE_LABEL.get(c, c)}: {n}" for c, n in sorted(roots.items(), key=lambda kv: -kv[1]))
    parts.append(f"""
<section>
  <h2>Where the time went</h2>
  <p>Each cycle's response is split into parts measured separately, so a miss can be blamed on the right cause.
  Runs of consecutive misses are attributed to what started them.</p>
  <div class="table-wrap"><table>
    <thead><tr><th>Worst value of</th><th>Before</th>{'<th>After</th>' if creps else ''}</tr></thead>
    <tbody>{prow}</tbody></table></div>
  <p><strong>What started each run of consecutive misses before the change</strong> (all {len(breps)}
  runs together): {e_(root_text or 'no misses')}.</p>
</section>""")

    # Diagnosis
    if diagnosis:
        res = diagnosis.get("result", diagnosis)
        fparts = []
        for f in res.get("findings", []):
            ev = "".join(f"<li>{e_(x)}</li>" for x in f["evidence"])
            un = "".join(f"<li>{e_(x)}</li>" for x in f["could_not_determine"])
            fparts.append(f"""
  <div class="finding"><div class="finding-head"><h3>{e_(f['title'])}</h3>
  <span class="conf c-{e_(f['confidence'])}">{e_(f['confidence'])} confidence</span></div>
  <p>{e_(f['explanation'])}</p>
  <h4>Evidence</h4><ul>{ev}</ul>
  <h4>Could not determine</h4><ul class="muted">{un}</ul>
  <p><strong>Suggested fix:</strong> {e_(f['suggested_fix'])}</p>{_trace_block(f)}</div>""")
        inh = res.get("inheritance_observed") or []
        parts.append(f"""
<section>
  <h2>Diagnosis</h2>
  <p>{e_(res.get('summary', ''))}</p>
  <p class="note">Diagnosis <code>{e_(diagnosis.get('diagnosis_id', ''))}</code>, taken
  {e_(diagnosis.get('taken', ''))} from {res.get('snapshots', '?')} thread-state snapshots of the target.</p>
  {''.join(fparts)}
  {f'<p>Priority inheritance was seen working {len(inh)} time(s) in that diagnosis.</p>' if inh else ''}
</section>""")

    # The change
    if p:
        a = e.get("approval") or {}
        hist = "".join(f"<li><span class='when'>{e_(h['time'])}</span> {e_(h['event'])}</li>"
                       for h in e.get("history", []))
        parts.append(f"""
<section>
  <h2>The change</h2>
  <p>{e_(p.get('description', ''))}</p>
  <pre class="diff">{_diff_html(p.get('diff', ''))}</pre>
  <p>{('Approved by a person via ' + e_(a['via']) + ' at ' + e_(a['time']) + ' for the diff with sha256 <code class="small">' + e_(a['diff_sha256'][:16]) + '…</code>.') if a else 'Not approved.'}</p>
  <h3>Timeline</h3><ul class="timeline">{hist}</ul>
</section>""")

    # Recommendation
    parts.append(f"<section><h2>Recommendation</h2><p>{e_(_recommendation(e, cmp, diagnosis))}</p></section>")

    # Limitations
    tl = ""
    for t in timing_checks or []:
        tl += (f"<li>Timing check of the target, CPU {t['cpu']} ({e_(t.get('measured', ''))}): "
               f"{t['wakeups']} timer wake-ups at a 1 ms period in {t['seconds']} s; mean lateness "
               f"{_ms(t['mean_late_us'], 2)}, worst {_ms(t['max_late_us'])}, {t['late_over_1ms']} "
               f"wake-ups more than 1 ms late, with nothing else of ours running.</li>")
    checks = [r.get("cross_check") for r in e["baseline"]["runs"] + (e.get("candidate") or {}).get("runs", [])]
    qchecks = [r.get("qbridged_check") for r in e["baseline"]["runs"] + (e.get("candidate") or {}).get("runs", [])]
    agree = (f"<li>Cross-checks: qbridge's statistics agreed with the program's own summary in "
             f"{sum(1 for c in checks if c)} of {len(checks)} runs, and with qbridged's message-passing "
             f"statistics in {sum(1 for c in qchecks if c)} of {len(qchecks)} runs.</li>")
    parts.append(f"""
<section>
  <h2>Limitations</h2>
  <ul>
    <li>These are finite runs ({len(breps)} before, {len(creps)} after). They show the effect of the change on this
    workload; they do not prove a worst-case response time or that the deadline can never be missed.</li>
    <li>The target is a QNX 8.0 virtual machine. Virtual-machine timing is noisier than real hardware, and absolute
    numbers vary between boots; only runs from the same session are compared.</li>
    {tl}
    <li>The workload is a simulated, educational demo. Nothing here is a claim about any automotive, medical or
    other safety-certified system.</li>
    {agree}
  </ul>
</section>""")

    # Provenance
    prov = "".join(f"<tr><td>{e_(side)} {i + 1}</td><td><code class='small'>{e_(r['run_id'])}</code></td>"
                   f"<td><code class='small'>{e_((r.get('binary_sha256') or '')[:16])}…</code></td></tr>"
                   for side, rl in (("Before", e["baseline"]["runs"]),
                                    ("After", (e.get("candidate") or {}).get("runs", [])))
                   for i, r in enumerate(rl))
    rb = e.get("rollback")
    parts.append(f"""
<section>
  <h2>Provenance</h2>
  <p>Baseline binary sha256 <code class="small">{e_(e['baseline']['binary_sha256'])}</code>{
      ('<br>Candidate binary sha256 <code class="small">' + e_(e['candidate']['binary_sha256']) + '</code>') if e.get('candidate') else ''}</p>
  <div class="table-wrap"><table><thead><tr><th>Run</th><th>Saved run id</th><th>Binary on target (sha256)</th></tr></thead>
  <tbody>{prov}</tbody></table></div>
  <p>Experiment state: <strong>{e_(e['state'])}</strong>{
      ('. Rolled back; the target binary matched the baseline sha256: ' + ('yes' if rb.get('matches_baseline') else 'NO')) if rb else ''}.
  Raw data: <code>~/.qbridge/experiments/{e_(e['id'])}/</code> and <code>~/.qbridge/runs/</code>.</p>
</section>""")

    return _PAGE.replace("{{TITLE}}", e_(f"qbridge RT report: {task}")).replace("{{BODY}}", "".join(parts))


def _diff_html(diff: str) -> str:
    out = []
    for line in diff.splitlines():
        cls = "add" if line.startswith("+") and not line.startswith("+++") else \
              "del" if line.startswith("-") and not line.startswith("---") else \
              "hunk" if line.startswith("@@") else ""
        out.append(f'<span class="{cls}">{e_(line)}</span>' if cls else e_(line))
    return "\n".join(out)


def _recommendation(e: dict, cmp: dict | None, diagnosis: dict | None) -> str:
    verdict = (cmp or {}).get("verdict")
    change = (e.get("proposal") or {}).get("description", "the change")
    if verdict == "improved":
        text = (f"Keep the change ({change}). Every measured run after it beat every run before it, "
                "with the worst case and misses taken first.")
        if e.get("state") == "rolled_back":
            text += (" In this experiment it was rolled back afterwards (for the demo); apply it again in "
                     "the real code.")
        return text + (" Re-measure on the real hardware before relying on the deadline, and keep "
                       "monitoring it in operation.")
    if verdict == "worse":
        return "Do not keep the change: every run after it was worse. Roll back to the baseline."
    if verdict == "inconclusive":
        return ("Do not rely on this change yet. The measurements before and after overlap, so they do "
                "not show that it helps. Run more repetitions or a longer workload, or look for another cause.")
    return "No recommendation yet: the change has not been measured."


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root { --bg:#f7f7f5; --card:#ffffff; --ink:#1d1d1f; --muted:#5f6368; --line:#e3e3df;
  --before:#c2410c; --after:#0f766e; --miss:#dc2626; --accent:#1d4ed8; --code:#f1f1ee;
  --good:#0f766e; --bad:#b91c1c; --warn:#a16207; }
@media (prefers-color-scheme: dark) { :root { --bg:#141416; --card:#1d1d20; --ink:#ececee;
  --muted:#a0a0a8; --line:#323238; --before:#fb923c; --after:#2dd4bf; --miss:#f87171;
  --accent:#93b4ff; --code:#26262b; --good:#34d399; --bad:#f87171; --warn:#fbbf24; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:960px; margin:0 auto; padding:28px 16px 64px; }
header h1 { font-size:26px; line-height:1.25; margin:4px 0 8px; letter-spacing:-0.01em; }
.eyebrow { color:var(--accent); font-weight:600; font-size:13px; text-transform:uppercase; letter-spacing:.06em; margin:0; }
.meta, .note, .muted { color:var(--muted); }
section { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px 22px; margin-top:16px; }
h2 { font-size:18px; margin:0 0 10px; } h3 { font-size:16px; margin:14px 0 6px; } h4 { font-size:14px; margin:12px 0 4px; }
code { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; background:var(--code); padding:1px 5px; border-radius:4px; font-size:13px; }
code.small { font-size:12px; }
.pill { display:inline-block; font-weight:700; text-transform:uppercase; letter-spacing:.05em; font-size:13px;
  padding:4px 10px; border-radius:999px; background:var(--code); }
.v-improved .pill { background:color-mix(in srgb, var(--good) 18%, transparent); color:var(--good); }
.v-worse .pill { background:color-mix(in srgb, var(--bad) 18%, transparent); color:var(--bad); }
.v-inconclusive .pill { background:color-mix(in srgb, var(--warn) 18%, transparent); color:var(--warn); }
.lead { font-size:16px; margin:10px 0 14px; }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px; }
.kpi { border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
.kpi-label { color:var(--muted); font-size:13px; }
.kpi-values { font-size:18px; font-weight:650; font-variant-numeric:tabular-nums;
  display:flex; flex-wrap:wrap; align-items:baseline; }
.before-v { color:var(--before); } .after-v { color:var(--after); } .arrow { color:var(--muted); margin:0 6px; }
.before-v, .after-v { white-space:nowrap; }
.kpi-note { color:var(--muted); font-size:12px; }
.table-wrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--muted); font-weight:600; font-size:13px; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.chart { margin:10px 0 18px; } .chart figcaption { font-size:14px; font-weight:600; margin-bottom:4px; }
svg { width:100%; height:auto; display:block; }
svg .grid { stroke:var(--line); stroke-width:1; } svg .tick, svg .axis { fill:var(--muted); font-size:11px; }
svg .deadline { stroke:var(--miss); stroke-width:1.5; stroke-dasharray:6 4; }
svg .deadline-label { fill:var(--miss); font-size:11px; }
svg .pts.before circle { fill:var(--before); } svg .pts.after circle { fill:var(--after); }
svg .miss circle { fill:var(--miss); }
svg .line { fill:none; stroke-width:2.2; } svg .line.before { stroke:var(--before); } svg .line.after { stroke:var(--after); }
.legend { font-size:13px; color:var(--muted); display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.key { display:inline-block; width:18px; height:3px; margin-left:10px; border-radius:2px; }
.key.before { background:var(--before); } .key.after { background:var(--after); }
.key.deadline { background:var(--miss); }
.finding { border-left:4px solid var(--accent); padding:4px 0 4px 14px; margin:12px 0; }
.finding-head { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.finding-head h3 { margin:6px 0; }
.conf { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; padding:2px 8px; border-radius:999px; background:var(--code); }
svg .st-run, .key.st-run { fill:var(--after); background:var(--after); }
svg .st-ready, .key.st-ready { fill:var(--warn); background:var(--warn); }
svg .st-mutex, .key.st-mutex { fill:var(--miss); background:var(--miss); }
svg .st-bg, .key.st-bg { fill:var(--line); background:var(--line); }
svg .lane { fill:var(--ink); font-size:12px; }
.key.st-run, .key.st-ready, .key.st-mutex { height:10px; }
ul.steps li.ok strong { color:var(--good); } ul.steps li.no strong { color:var(--bad); }
.trace-ok { color:var(--good); font-weight:600; } .trace-no { color:var(--warn); font-weight:600; }
.c-high { color:var(--good); } .c-medium { color:var(--warn); } .c-low { color:var(--muted); }
pre.diff { background:var(--code); padding:12px; border-radius:8px; overflow-x:auto; font-size:13px; }
pre.diff .add { color:var(--good); } pre.diff .del { color:var(--bad); } pre.diff .hunk { color:var(--accent); }
ul.timeline { list-style:none; padding:0; } ul.timeline li { padding:3px 0; }
.when { font-family:ui-monospace,Consolas,monospace; color:var(--muted); font-size:13px; margin-right:8px; }
@media (max-width:600px) { header h1 { font-size:21px; } section { padding:16px; } }
</style></head>
<body><main>{{BODY}}</main></body></html>
"""
