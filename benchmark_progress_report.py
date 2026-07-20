#!/usr/bin/env python3
"""
Interim benchmark progress report — safe to run mid-run, reports on whatever runs
exist so far (not a final scorecard).

Per-model pass/fail table (like the report image) + a per-task complexity table,
built straight from a run folder's output.log + reward.json files.

Pass/fail comes from each run's reward.json (reward == 1 => pass). Effort signals
(steps, tool calls, output tokens, duration) come from output.log and feed
task_complexity — the SAME min-max-normalized 0-10 score used elsewhere.

Two tables:
  * Per-model: Pass / Fail / Pass Rate, grouped by TASK — a task counts as a pass if the
    model solved it in at least one run (pass@k over its runs), not per individual run.
  * Per-task:  Complexity + Pass / Fail / Pass Rate pooled across all models.

Usage: python3 benchmark_progress_report.py [RUN_DIR] [--no-billing]
  RUN_DIR defaults to the newest folder under results/. Refreshes billing.json first so
  the cost column is the real GLM bill; pass --no-billing to skip that (uses modeled cost).
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Vendored engine — log parsing, concurrency-aware cost attribution, complexity, and Modal billing.
# Previously split across aggregate.py / billing.py; inlined here so this is the SINGLE report file.
# Same math (guarded by verify_report.py). These produce the money numbers — edit with care.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

# Hourly run-rate of the 8×B200 auto-endpoint while actively serving — the MODELED fallback used only
# when billing.json is absent (measured $50.7/hr from a single-run bill). Real runs override this with
# the effective hourly derived from billing.json.
GPU_HOURLY_USD = 50.7


def load_usage(outdir):
    """(tokens_in, tokens_out, ccusage_cost) for a run from usage.json. tokens_in TOTAL = fresh input
    + cache-creation + cache-read (comparable across providers). Returns (0, 0, 0.0) if absent."""
    try:
        with open(os.path.join(outdir, "usage.json")) as f:
            sessions = json.load(f).get("sessions", [])
    except Exception:
        return 0, 0, 0.0
    tin = sum(int(s.get("inputTokens", 0) or 0) + int(s.get("cacheCreationTokens", 0) or 0)
              + int(s.get("cacheReadTokens", 0) or 0) for s in sessions)
    tout = sum(int(s.get("outputTokens", 0) or 0) for s in sessions)
    cost = sum(float(s.get("totalCost", 0) or 0) for s in sessions)
    return tin, tout, cost


def log_stats(outdir):
    """Parse output.log (opencode JSON-lines) into work/efficiency metrics for a run:
      call_s  - endpoint generation time: sum over steps of (step_finish - step_start), the FULL
                request->response wall-clock per call INCLUDING token streaming, excluding local
                tool/script gaps between steps (pip/pytest/git).
      steps/tools/prose/reason/out - assistant turns, tool calls, prose chars, reasoning/output tokens."""
    log = os.path.join(outdir, "output.log")
    m = {"call_s": 0.0, "steps": 0, "tools": 0, "prose": 0, "reason": 0, "out": 0}
    if not os.path.exists(log):
        return m
    call_ms = 0
    start = None
    for line in open(log):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t, ts, p = ev.get("type"), ev.get("timestamp"), ev.get("part", {})
        if t == "step_start":
            start, m["steps"] = ts, m["steps"] + 1
        elif t == "tool_use":
            m["tools"] += 1
        elif t == "text":
            m["prose"] += len(p.get("text", "") or "")
        elif t == "step_finish":
            tk = p.get("tokens", {}) or {}
            m["out"] += tk.get("output", 0) or 0
            m["reason"] += tk.get("reasoning", 0) or 0
            if start is not None and ts is not None:
                call_ms += ts - start
            start = None
    m["call_s"] = call_ms / 1000.0
    return m


def claude_stats(outdir):
    """Parse a Claude Code run's stream-json output.log: native cost/usage/turns (from the final
    `result` event) plus tool-call and prose counts. A killed/timed-out run never emits `result`,
    so cost is left None (unknown), not zero."""
    log = os.path.join(outdir, "output.log")
    m = {"cost": 0.0, "tin": 0, "tout": 0, "call_s": 0.0,
         "steps": 0, "tools": 0, "prose": 0, "reason": 0, "out": 0}
    if not os.path.exists(log):
        return m
    for line in open(log):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        if t == "assistant":
            m["steps"] += 1
            for b in ev.get("message", {}).get("content", []) or []:
                bt = b.get("type")
                if bt == "tool_use":
                    m["tools"] += 1
                elif bt == "text":
                    m["prose"] += len(b.get("text", "") or "")
        elif t == "result":
            u = ev.get("usage", {}) or {}
            m["tin"] = sum(int(u.get(k, 0) or 0) for k in
                           ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            m["tout"] = m["out"] = int(u.get("output_tokens", 0) or 0)
            m["cost"] = float(ev.get("total_cost_usd", 0) or 0)
            m["call_s"] = (ev.get("duration_api_ms", 0) or 0) / 1000.0
            m["has_result"] = True
            if ev.get("num_turns"):
                m["steps"] = ev["num_turns"]
    if not m.get("has_result"):
        m["cost"] = None
    return m


def gen_intervals(outdir):
    """Absolute-epoch (start, end) windows the model spent GENERATING, per step (step_start ->
    step_finish = full request->response incl. streaming), from an opencode output.log. Unioning
    these across concurrent runs gives concurrency-correct generation wall-clock."""
    log = os.path.join(outdir, "output.log")
    out = []
    if not os.path.exists(log):
        return out
    start = None
    for line in open(log):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t, ts = ev.get("type"), ev.get("timestamp")
        if t == "step_start":
            start = ts
        elif t == "step_finish" and start is not None and ts is not None:
            out.append((start / 1000.0, ts / 1000.0))
            start = None
    return out


def union_seconds(intervals):
    """Wall-clock seconds covered by the union of (start, end) intervals — overlapping/parallel runs
    merged, so 3 runs sharing a 3-min window count as 3 min, not 9 (you pay for GPU uptime)."""
    total = 0.0
    cur_s = cur_e = None
    for s, e in sorted(intervals):
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    if cur_s is not None:
        total += cur_e - cur_s
    return total


def attribute_cost(owned_intervals, rate_per_sec):
    """Split an endpoint bill across the calls that ran on it, by who was busy when.

    Every instant costs `rate_per_sec`, split EQUALLY among the calls active right then: a call alone
    for 10s at $1/s costs $10; two calls sharing 10s cost $5 each ($10 total, not $20). The sum over
    owners always equals rate × union(windows) — never double-counted for parallelism.
    Returns {owner: dollars}. Sweep line over interval endpoints (exact, not per-second)."""
    ivs = [(s, e, o) for (s, e, o) in owned_intervals if e > s]
    out = defaultdict(float)
    if not ivs:
        return out
    pts = sorted({s for s, _e, _o in ivs} | {e for _s, e, _o in ivs})
    for a, b in zip(pts, pts[1:]):
        dur = b - a
        if dur <= 0:
            continue
        active = [o for (s, e, o) in ivs if s < b and e > a]
        if not active:
            continue
        share = dur * rate_per_sec / len(active)
        for o in active:
            out[o] += share
    return out


def task_complexity(task_stat):
    """Empirical, RELATIVE complexity per task from observed effort pooled across all runs/models:
    mean steps, tool calls, output tokens, duration — each min-max normalized across the set,
    averaged, scaled to 0-10. pass_rate reported alongside (outcome, kept separate)."""
    tasks = {}
    for t, a in task_stat.items():
        n = max(a["runs"], 1)
        tasks[t] = {"runs": a["runs"], "pass_rate": round(a["pass"] / n, 3),
                    "avg_steps": a["steps"] / n, "avg_tools": a["tools"] / n,
                    "avg_out": a["out"] / n, "avg_dur": a["dur"] / n}
    sig = ["avg_steps", "avg_tools", "avg_out", "avg_dur"]
    lo = {s: min(tasks[t][s] for t in tasks) for s in sig} if tasks else {}
    hi = {s: max(tasks[t][s] for t in tasks) for s in sig} if tasks else {}
    for t in tasks:
        norm = [(tasks[t][s] - lo[s]) / (hi[s] - lo[s]) if hi[s] > lo[s] else 0.0 for s in sig]
        tasks[t]["complexity"] = round(10 * sum(norm) / len(norm), 1)
    return tasks


def peak_concurrency(ivals):
    """Max generation windows open at the same instant (sweep line). Touching intervals (one ends
    exactly as the next starts) do NOT count as overlapping."""
    ev = []
    for s, e in ivals:
        if s is None or e is None or e < s:
            continue
        ev.append((s, 1)); ev.append((e, -1))
    ev.sort(key=lambda x: (x[0], x[1]))
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    return peak


def avg_concurrency(ivals):
    """Time-weighted mean concurrent generation windows over the union wall-clock."""
    ev = []
    for s, e in ivals:
        if s is None or e is None or e < s:
            continue
        ev.append((s, 1)); ev.append((e, -1))
    if not ev:
        return 0.0
    ev.sort(key=lambda x: (x[0], x[1]))
    cur = 0
    prev = None
    area = 0.0
    for t, d in ev:
        if prev is not None and cur > 0 and t > prev:
            area += cur * (t - prev)
        cur += d
        prev = t
    wall = union_seconds(ivals)
    return area / wall if wall else 0.0


# ---- Modal billing (inlined from billing.py) — the REAL endpoint $ over GLM-active time -----------
def _bill_is_self_hosted(model):
    return (model or "").lower().split("/")[0].startswith("modal")


def _bill_glm_intervals(results_dir):
    """Absolute-epoch (start, end) windows of self-hosted (GLM) jobs only, from manifest.csv."""
    ivs = []
    with open(os.path.join(results_dir, "manifest.csv")) as f:
        for r in csv.DictReader(f):
            if not _bill_is_self_hosted(r.get("model")):
                continue
            try:
                s, e = float(r["start"]), float(r["end"])
            except (TypeError, ValueError, KeyError):
                continue
            if e > s:
                ivs.append((s, e))
    return ivs


def _bill_merge(ivs):
    out = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _bill_covered_seconds(merged, a, b):
    tot = 0.0
    for s, e in merged:
        lo, hi = max(a, s), min(b, e)
        if hi > lo:
            tot += hi - lo
    return tot


def refresh_billing(results_dir, app="ep-Modal-Auto-Endpoints"):
    """Pull the ACTUAL Modal bill for the GLM endpoint, counting ONLY GLM-active time (union of GLM
    job windows), and write <results_dir>/billing.json. Best-effort: needs `modal` + Modal creds
    (MODAL_TOKEN_ID/SECRET). Returns the dict, or None if there are no GLM windows / on failure."""
    from datetime import datetime, timedelta, timezone
    import modal
    ivs = _bill_glm_intervals(results_dir)
    if not ivs:
        return None
    merged = _bill_merge(ivs)
    s, e = merged[0][0], merged[-1][1]
    start = datetime.fromtimestamp(s, tz=timezone.utc)
    end = datetime.fromtimestamp(e, tz=timezone.utc)
    q0 = start.replace(minute=0, second=0, microsecond=0)
    q1 = end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    rep = modal.Workspace.from_context().billing.report(start=q0, end=q1, resolution="h")
    total, active_s, by_hour = 0.0, 0.0, []
    for it in rep:
        if it.description != app:
            continue
        h0 = it.interval_start
        if h0.tzinfo is None:
            h0 = h0.replace(tzinfo=timezone.utc)
        h1 = h0 + timedelta(hours=1)
        cov = _bill_covered_seconds(merged, h0.timestamp(), h1.timestamp())
        if cov <= 0:
            continue
        frac = cov / 3600.0
        hour_cost = float(it.cost or 0)
        billed = hour_cost * frac
        total += billed
        active_s += cov
        by_hour.append({"hour": h0.isoformat(), "hour_cost": round(hour_cost, 4),
                        "glm_seconds_in_hour": round(cov), "billed": round(billed, 4)})
    active_h = active_s / 3600.0
    out = {"app": app, "window_start": start.isoformat(), "window_end": end.isoformat(),
           "window_hours": round(active_h, 3), "cost": round(total, 4),
           "effective_hourly": round(total / active_h, 4) if active_h else None, "by_hour": by_hour}
    with open(os.path.join(results_dir, "billing.json"), "w") as f:
        json.dump(out, f, indent=2)
    return out

# ══════════════════════════════════════════════════════════════════════════════════════════════════

# dir-name model prefix -> display label (matches the report image)
MODEL_LABEL = {
    "opus": "Claude Opus 4.8",
    "glm-default": "GLM-5.2 (default)",
    "glm-high": "GLM-5.2 (high reasoning)",
    "glm-nothink": "GLM-5.2 (no thinking)",
}
# stable row order for the per-model table
MODEL_ORDER = ["opus", "glm-default", "glm-high", "glm-nothink"]


def parse_dirname(name):
    """'glm-default__abs-module-cache-flags__run3' -> (model, task, run)."""
    parts = name.split("__")
    return parts[0], "__".join(parts[1:-1]), parts[-1]


def run_duration_s(rundir, is_claude):
    """Wall-clock seconds for a run from its output.log.
    Claude stream-json: duration_ms in the final `result` event.
    opencode (GLM): span between first and last event timestamp (ms)."""
    log = os.path.join(rundir, "output.log")
    if not os.path.exists(log):
        return 0.0
    if is_claude:
        for line in open(log):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") == "result":
                return (ev.get("duration_ms", 0) or 0) / 1000.0
        return 0.0
    first = last = None
    for line in open(log):
        try:
            ts = json.loads(line).get("timestamp")
        except Exception:
            continue
        if ts is None:
            continue
        first = ts if first is None else first
        last = ts
    return (last - first) / 1000.0 if (first is not None and last is not None) else 0.0


def is_pass(rundir):
    try:
        return json.load(open(os.path.join(rundir, "reward.json"))).get("reward") == 1
    except Exception:
        return False


def glm_rate(run_dir):
    """GLM $/hr for costing. Uses the REAL effective hourly from billing.json (the GLM-only actual
    Modal bill ÷ GLM-active hours) when it exists; otherwise falls back to aggregate's modeled rate.
    Returns (rate_per_hour, is_real)."""
    try:
        bj = json.load(open(os.path.join(run_dir, "billing.json")))
        if bj.get("effective_hourly"):
            return float(bj["effective_hourly"]), True
    except Exception:
        pass
    return GPU_HOURLY_USD, False


def collect(run_dir):
    """Walk run_dir's top-level run folders -> per-run records, pooled task_stat, and a
    per-(task, model) [passes, total] cell map for the matrix."""
    runs = []
    # task_stat shape expected by task_complexity, keyed by task here (no prompt dim)
    task_stat = defaultdict(lambda: {"runs": 0, "steps": 0, "tools": 0, "out": 0, "dur": 0.0,
                                     "pass": 0, "cost": 0.0, "cost_n": 0})
    cell = defaultdict(lambda: [0, 0])   # (task, model) -> [passes, total]
    rate_hr, cost_is_real = glm_rate(run_dir)   # real $/hr from billing.json when present
    for entry in sorted(os.listdir(run_dir)):
        rundir = os.path.join(run_dir, entry)
        if not os.path.isdir(rundir) or not os.path.exists(os.path.join(rundir, "reward.json")):
            continue
        model, task, _run = parse_dirname(entry)
        is_claude = model == "opus"
        pm = claude_stats(rundir) if is_claude else log_stats(rundir)
        dur = run_duration_s(rundir, is_claude)
        # Claude reports its own $ (None if the run was killed before its result event);
        # GLM is self-hosted -> $ = API-call seconds x GPU rate.
        cost = pm.get("cost") if is_claude else (pm["call_s"] / 3600.0 * rate_hr)
        passed = is_pass(rundir)
        # per-run generation windows (GLM only) so main() can split the real bill by concurrency
        ivs = [] if is_claude else gen_intervals(rundir)
        tin = pm["tin"] if is_claude else load_usage(rundir)[0]   # tokens_in (opus native / GLM ccusage)
        runs.append({"model": model, "task": task, "run": _run, "passed": passed, "cost": cost,
                     "label": entry, "ivs": ivs,
                     # 'sole' = this run priced ALONE (before the concurrency split overwrites 'cost');
                     # keeps the per_run.csv sole_usd column and the summary sole totals.
                     # three nested times: elapsed (agent session) ⊇ generation (GPU/API gen = cost basis).
                     "sole": cost, "elapsed": dur, "generation": pm.get("call_s", 0.0),
                     "steps": pm["steps"], "tin": tin, "tout": pm["out"]})
        a = task_stat[task]
        a["runs"] += 1
        a["steps"] += pm["steps"]; a["tools"] += pm["tools"]; a["out"] += pm["out"]
        a["dur"] += dur
        a["pass"] += 1 if passed else 0
        if cost is not None:
            a["cost"] += cost; a["cost_n"] += 1
        c = cell[(task, model)]
        c[1] += 1
        c[0] += 1 if passed else 0
    return runs, task_stat, cell, cost_is_real


def _step(i, n, msg):
    """One-line console progress: [ date ] i/n msg (done)."""
    print(f"[ {datetime.now():%Y-%m-%d %H:%M:%S} ] {i}/{n} {msg} (done)", flush=True)


_HTML_CSS = """
:root{--bg:#f6f7f9;--fg:#16181d;--muted:#6b7280;--card:#fff;--border:#e5e7eb;--head:#f9fafb;
 --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
 --g-bg:#ecfdf5;--g-fg:#15803d;--a-bg:#fef3c7;--a-fg:#92620a;--r-bg:#fef2f2;--r-fg:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#0e1014;--fg:#e6e8ec;--muted:#9aa1ad;--card:#161a21;
 --border:#282d38;--head:#1b2028;
 --g-bg:#0d2a1e;--g-fg:#9fe1cb;--a-bg:#3a2a0a;--a-fg:#fac775;--r-bg:#3a1414;--r-fg:#f0997b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:34px 20px 90px}
h1{font-size:23px;margin:0 0 3px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 26px;font-size:13px}
h2{font-size:14px;margin:30px 0 8px;font-weight:600}
.tw{overflow-x:auto;border:1px solid var(--border);border-radius:12px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:10px 15px;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap}
thead th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600;background:var(--head)}
tbody tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-family:var(--mono)}
td.c{text-align:center}
.cell{font-family:var(--mono);font-weight:600;padding:3px 9px;border-radius:6px;display:inline-block;min-width:34px;text-align:center}
.g{background:var(--g-bg);color:var(--g-fg)}.a{background:var(--a-bg);color:var(--a-fg)}
.r{background:var(--r-bg);color:var(--r-fg)}.z{color:var(--muted)}
.bar{background:var(--border);border-radius:4px;height:6px;width:56px;display:inline-block;vertical-align:middle;overflow:hidden;margin-left:8px}
.bar>i{display:block;height:100%;background:#2a78d6}
.legend{display:flex;gap:14px;font-size:12px;color:var(--muted);margin:10px 2px 0}
.sw{width:12px;height:12px;border-radius:3px;display:inline-block;margin-right:5px;vertical-align:-2px}
.take{background:var(--g-bg);color:var(--g-fg);border-radius:10px;padding:11px 15px;font-size:13.5px;font-weight:600;margin:0 0 18px}
.explain{border:1px solid var(--border);background:var(--card);border-radius:12px;padding:14px 17px;margin:0 0 26px;font-size:13px;line-height:1.55}
.explain b{color:var(--fg)}
.explain ul{margin:9px 0 0;padding-left:0;list-style:none}
.explain li{margin:7px 0;color:var(--muted)}
.explain li b{font-family:var(--mono)}
.explain .eg{margin:8px 0 2px;color:var(--muted)}
.explain .eg code{font-family:var(--mono);font-size:14px;letter-spacing:2px;background:var(--head);padding:2px 8px;border-radius:6px;margin-left:6px}
.explain .read{display:block;margin-top:11px;color:var(--fg);font-weight:600}
footer{color:var(--muted);font-size:12px;margin-top:40px}
"""


def _cell_cls(passes, total):
    if total == 0:
        return "z", "·"
    r = passes / total
    cls = "g" if r >= 0.75 else "a" if r >= 0.45 else "r"
    return cls, f"{passes}/{total}"


def write_html(path, run_dir, runs, model_rows, comp, cell, cost_is_real=False,
               peak_c=0, avg_c=0.0, takeaway="", run_rows=None):
    """Self-contained progress_report.html — the same three tables as the chat visualization."""
    import html as _h
    esc = lambda x: _h.escape(str(x))
    models = [m for m in MODEL_ORDER if any(k[1] == m for k in cell)]
    tasks_by_comp = sorted(comp, key=lambda t: -comp[t]["complexity"])

    # per-model rows
    mbody = ""
    for label, p, f, rate, cost, per_run, per_solve in model_rows:
        frac = (p / (p + f)) if (p + f) else 0
        mbody += (f"<tr><td><b>{esc(label)}</b></td><td class=num>{p}</td><td class=num>{f}</td>"
                  f"<td class=num>{esc(rate)}<span class=bar><i style='width:{frac*100:.0f}%'></i></span></td>"
                  f"<td class=num>{esc(cost)}</td><td class=num>{esc(per_run)}</td>"
                  f"<td class=num>{esc(per_solve)}</td></tr>")

    # per-run rows (cost breakdown — every attempt, not grouped by task)
    rbody = ""
    for label, n, p, fl, passrate, total, passc, failc, perpass in (run_rows or []):
        frac = (p / n) if n else 0
        rbody += (f"<tr><td><b>{esc(label)}</b></td><td class=num>{n}</td><td class=num>{p}</td>"
                  f"<td class=num>{fl}</td>"
                  f"<td class=num>{esc(passrate)}<span class=bar><i style='width:{frac*100:.0f}%'></i></span></td>"
                  f"<td class=num>{esc(total)}</td>"
                  f"<td class=num>{esc(passc)}</td><td class=num>{esc(failc)}</td>"
                  f"<td class=num>{esc(perpass)}</td></tr>")

    # per-task rows
    tbody = ""
    for t in tasks_by_comp:
        c = comp[t]
        p = int(round(c["pass_rate"] * c["runs"]))
        tbody += (f"<tr><td>{esc(t)}</td><td class=num>{c['complexity']:.1f}</td>"
                  f"<td class=num>{p}</td><td class=num>{c['runs']-p}</td>"
                  f"<td class=num>{100*c['pass_rate']:.1f}%</td></tr>")

    # matrix rows
    mx_head = "".join(f"<th class=c>{esc(MODEL_LABEL.get(m, m))}</th>" for m in models)
    mx_body = ""
    for t in tasks_by_comp:
        cells = ""
        for m in models:
            cls, txt = _cell_cls(*cell[(t, m)])
            cells += f"<td class=c><span class='cell {cls}'>{txt}</span></td>"
        mx_body += (f"<tr><td>{esc(t)}</td><td class=num>{comp[t]['complexity']:.1f}</td>{cells}</tr>")

    doc = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Benchmark progress — {esc(os.path.basename(run_dir))}</title>
<style>{_HTML_CSS}</style></head><body><div class=wrap>
<h1>Benchmark progress <span style="color:var(--muted);font-weight:400">· interim</span></h1>
<p class=sub>{esc(run_dir)} · {len(runs)} runs so far · {len(comp)} tasks · peak {peak_c} concurrent · avg {avg_c}×</p>
{f'<p class=take>{esc(takeaway)}</p>' if takeaway else ''}

<div class=explain>
<b>Two success rates — how to read them.</b> Every task is attempted up to 4 times (independent runs).
<div class=eg>Example — one task, its 4 attempts: <code>✗ ✗ ✗ ✓</code></div>
<ul>
<li><b>pass@1</b> — <b>single-shot</b> success: the share of <i>individual attempts</i> that pass. This task = 1 of 4 = <b>25%</b>. Pooled over every attempt it gives the <i>per-run</i> table below. This is the number leaderboards (SWE-bench, DeepSWE) usually report.</li>
<li><b>pass@k</b> — <b>best-of-k</b>: whether the model solved the task in <i>at least one</i> of its attempts. This task = solved ✓ (it passed once). Pooled over tasks it gives the <i>per-task</i> table below. Higher, because retries are folded in.</li>
</ul>
<span class=read>Read it as: “with 4 attempts GLM-5.2 (high) solves 81% of tasks (pass@k); a single attempt succeeds 47% of the time (pass@1).”</span>
</div>

<h2>Per model — tasks solved (passed in ≥1 run) / unsolved + cost</h2>
<div class=tw><table><thead><tr><th>Model</th><th class=num>Pass</th><th class=num>Fail</th>
<th class=num>pass@k</th><th class=num>Total $</th><th class=num>Avg $/run</th><th class=num>$/solve</th></tr></thead><tbody>{mbody}</tbody></table></div>
<p style="color:var(--muted);font-size:12px;margin:8px 2px 0"><b>pass@k</b> = tasks solved in ≥1 of their (≈4) runs ÷ total tasks — best-of-k, retries allowed · <b>Total&nbsp;$</b> = sum over all its runs · <b>Avg&nbsp;$/run</b> = Total ÷ number of runs (mean cost of one attempt) · <b>$/solve</b> = Total ÷ tasks solved (pass@k) — so it folds in the failed attempts.</p>

<h2>Per model — per run: cost breakdown (every attempt, not grouped by task)</h2>
<div class=tw><table><thead><tr><th>Model</th><th class=num>Runs</th><th class=num>Pass</th>
<th class=num>Fail</th><th class=num>pass@1</th><th class=num>Total $</th><th class=num>$ pass</th><th class=num>$ fail</th><th class=num>$/pass&nbsp;run</th></tr></thead><tbody>{rbody}</tbody></table></div>
<p style="color:var(--muted);font-size:12px;margin:8px 2px 0"><b>pass@1</b> = passing runs ÷ total runs — the single-shot success rate (each attempt scored on its own; this is the DeepSWE-comparable number) · <b>Total&nbsp;$</b> = all runs = <b>$&nbsp;pass</b> (spent on passing runs) + <b>$&nbsp;fail</b> (burned on failing runs) · <b>$/pass&nbsp;run</b> = Total ÷ passing runs — the all-in cost of one successful attempt, with the failed retries folded in.</p>

<h2>Per task — complexity (0–10, hardest first) and pass / fail across all models</h2>
<div class=tw><table><thead><tr><th>Task</th><th class=num>Complexity</th><th class=num>Pass</th>
<th class=num>Fail</th><th class=num>Pass rate</th></tr></thead><tbody>{tbody}</tbody></table></div>

<h2>Passes per task × model</h2>
<div class=tw><table><thead><tr><th>Task</th><th class=num>Complexity</th>{mx_head}</tr></thead>
<tbody>{mx_body}</tbody></table></div>
<div class=legend><span><span class="sw g"></span>≥75% pass</span>
<span><span class="sw a"></span>45–75%</span><span><span class="sw r"></span>&lt;45%</span>
<span><span class="sw" style="background:var(--border)"></span>no runs</span></div>

<footer><b>$ total</b> = a model's spend across all its runs. <b>$/run</b> = that ÷ its runs (mean cost
of one attempt — <i>mean is outlier-sensitive: a few long max/no-think runs pull it up</i>).
<b>$/solved</b> = total ÷ tasks solved (pass@k) — what it costs to land a task, retries and unsolved-task
spend included. They chain: $/solved = $/run × runs ÷ solved. Cost basis:
{"real — the actual Modal bill from billing.json (GLM) / Claude Code's reported $ (Opus)" if cost_is_real
 else f"modeled ~${GPU_HOURLY_USD:.0f}/hr for GLM (no billing.json) / Claude Code's reported $ for Opus"}.
Complexity: min-max-normalized effort (steps, tools, output tokens, duration) pooled across all runs,
scaled 0–10 — relative to this task set. Interim progress report (benchmark may still be running).
Generated by benchmark_progress_report.py.</footer>
</div></body></html>"""
    with open(path, "w") as f:
        f.write(doc)
    return path


def main():
    argv = [a for a in sys.argv[1:] if a != "--no-billing"]
    no_billing = "--no-billing" in sys.argv
    default = None
    base = Path("results")
    if base.is_dir():
        subs = sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)
        default = str(subs[0]) if subs else None
    run_dir = argv[0] if argv else default
    if not run_dir or not os.path.isdir(run_dir):
        sys.exit(f"run dir not found: {run_dir!r} — pass one as an argument")

    STEPS = 5  # billing → collect → costs → csv → html  (tables live in HTML only)

    # Refresh billing.json first so the cost column reflects the REAL GLM bill. Best-effort: needs
    # modal + Modal creds (MODAL_TOKEN_ID/SECRET). Safe mid-run — it reads a manifest snapshot and
    # bills only GLM-active time so far. On any failure the report falls back to modeled cost.
    if not no_billing:
        try:
            refresh_billing(run_dir)
        except Exception:
            pass
    _step(1, STEPS, "billing")

    runs, task_stat, cell, cost_is_real = collect(run_dir)
    if not runs:
        sys.exit(f"no run folders (with reward.json) under {run_dir}")
    _step(2, STEPS, f"collect {len(runs)} runs / {len(task_stat)} tasks")

    # ---- attribute the REAL endpoint bill to GLM runs by concurrency -----------------------------
    # collect() priced each GLM run "sole" (its own generation-seconds × rate). But the endpoint is
    # SHARED: several runs stream at once, and Modal bills the GPU wall-clock ONCE, not per concurrent
    # request. So we re-price GLM by concurrency: split every generating second among the
    # runs busy in that instant (attribute_cost) — a run that ran mostly alone keeps more of the bill
    # than one buried in a pack; the sum over runs is exactly rate × union(generation windows), never
    # double-counted for parallelism. The per-second rate is PINNED so the GLM total equals the real
    # bill (billing.json `cost`) to the cent when it exists; otherwise it falls back to the modeled GPU
    # rate (still concurrency-split). Opus is per-token (its own real charge) and is left untouched.
    owned = [(s, e, r["label"]) for r in runs if r["model"] != "opus" for (s, e) in r["ivs"]]
    gen_union = union_seconds([(s, e) for (s, e, _o) in owned])
    bill = None
    if cost_is_real:
        try:
            with open(os.path.join(run_dir, "billing.json")) as bf:
                bill = float(json.load(bf)["cost"])
        except Exception:
            pass
    if gen_union > 0:
        rate_per_sec = (bill / gen_union) if bill else (GPU_HOURLY_USD / 3600.0)
        billed = attribute_cost(owned, rate_per_sec)   # {run label: $}, sums to rate×gen_union
        for r in runs:
            if r["model"] != "opus":
                r["cost"] = billed.get(r["label"], 0.0)
        # rebuild task_stat's per-task cost from the attributed run costs so the difficulty csv agrees
        for a in task_stat.values():
            a["cost"], a["cost_n"] = 0.0, 0
        for r in runs:
            if r.get("cost") is not None:
                a = task_stat[r["task"]]
                a["cost"] += r["cost"]; a["cost_n"] += 1
    _step(3, STEPS, "attribute costs")

    comp = task_complexity(task_stat)   # {task: {complexity, pass_rate, ...}}

    # ---- per-model table: pass@k — a TASK counts as pass if the model solved it in ANY of its runs
    # (fail,fail,fail,pass -> pass; fail,fail,fail -> fail). So Pass/Fail count tasks, not runs. ----
    solved = defaultdict(bool)        # (model, task) -> solved in at least one run
    model_cost = defaultdict(float)   # total $ each model spent across ALL its runs (incl. fails)
    model_runs = defaultdict(int)     # graded runs (attempts) per model
    for r in runs:
        solved[(r["model"], r["task"])] |= bool(r["passed"])
        model_runs[r["model"]] += 1
        if r.get("cost") is not None:
            model_cost[r["model"]] += r["cost"]
    per_model = defaultdict(lambda: {"pass": 0, "fail": 0})
    for (model, _task), ok in solved.items():
        per_model[model]["pass" if ok else "fail"] += 1
    rows = []
    for model in MODEL_ORDER + [m for m in per_model if m not in MODEL_ORDER]:
        if model not in per_model:
            continue
        s = per_model[model]
        tot = s["pass"] + s["fail"]
        cost = model_cost.get(model, 0.0)
        nruns = model_runs.get(model, 0)
        # all three share $Total so they reconcile: $/Solved = $/Run × (runs ÷ solved).
        rows.append([
            MODEL_LABEL.get(model, model), s["pass"], s["fail"],
            f"{100 * s['pass'] / tot:.1f}%" if tot else "-",
            f"${cost:.2f}",                                     # $ Total: all spend
            f"${cost / nruns:.2f}" if nruns else "-",           # $/Run: mean per attempt
            f"${cost / s['pass']:.2f}" if s["pass"] else "-",   # $/Solved: total ÷ tasks solved (pass@k)
        ])

    # ---- per-model, per RUN (ungrouped): cost breakdown — where the money went, and $ per pass ----
    run_stat = defaultdict(lambda: {"runs": 0, "pass": 0, "total": 0.0, "pass_c": 0.0, "fail_c": 0.0})
    for r in runs:
        rs = run_stat[r["model"]]
        rs["runs"] += 1
        c = r.get("cost") or 0.0
        rs["total"] += c
        if r["passed"]:
            rs["pass"] += 1; rs["pass_c"] += c
        else:
            rs["fail_c"] += c
    run_rows = []
    for model in MODEL_ORDER + [m for m in run_stat if m not in MODEL_ORDER]:
        if model not in run_stat:
            continue
        rs = run_stat[model]
        n, p = rs["runs"], rs["pass"]
        run_rows.append([
            MODEL_LABEL.get(model, model), n, p, n - p,
            f"{100 * p / n:.1f}%" if n else "-",            # pass@1: passing runs ÷ runs (single-shot)
            f"${rs['total']:.2f}",                          # Total $ (all runs) = $ pass + $ fail
            f"${rs['pass_c']:.2f}",                         # $ that went into passing runs
            f"${rs['fail_c']:.2f}",                         # $ burned on failing runs
            f"${rs['total'] / p:.2f}" if p else "-",        # $/pass = Total ÷ passing runs
        ])

    # ---- deepswe_task_difficulty.csv (into the run folder), hardest first ----
    csv_path = os.path.join(run_dir, "deepswe_task_difficulty.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["task", "complexity", "trials", "passes", "pass_rate_pct", "avg_cost_usd"])
        for task in sorted(task_stat, key=lambda t: (task_stat[t]["pass"] / max(task_stat[t]["runs"], 1))):
            a = task_stat[task]
            avg_cost = a["cost"] / a["cost_n"] if a["cost_n"] else ""
            w.writerow([task, comp[task]["complexity"], a["runs"], a["pass"],
                        round(100 * a["pass"] / max(a["runs"], 1), 1),
                        round(avg_cost, 2) if avg_cost != "" else ""])

    # ---- per_run.csv + summary.csv --------------------------------------------------------------
    # Regenerated from THESE verified numbers, so they match the HTML tables and reconcile to the real
    # bill (billed_usd sums to billing.json). 'billed_usd' = concurrency-split real cost; 'sole_usd' =
    # the same run priced alone (the old inflated basis) — kept for comparison. Three nested times per
    # run: orchestration_s (full pier job: build+agent+grade+teardown) ⊇ session_s (agent session,
    # first→last log event) ⊇ generation_s (GPU/API generation = Σ step_start→step_finish = the COST
    # BASIS). billed_usd is derived from generation_s split by concurrency, not from the wall-clocks.
    def _mord(m):
        return MODEL_ORDER.index(m) if m in MODEL_ORDER else len(MODEL_ORDER)
    man = {}   # label -> manifest row, for orchestration_s + start/end (the job wall-clock)
    mpath = os.path.join(run_dir, "manifest.csv")
    if os.path.exists(mpath):
        for row in csv.DictReader(open(mpath)):
            man[os.path.basename((row.get("outdir") or "").rstrip("/"))] = row
    pr_path = os.path.join(run_dir, "per_run.csv")
    with open(pr_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["task", "setup", "run", "passed", "start", "end",
                    "orchestration_s", "session_s", "generation_s",
                    "billed_usd", "sole_usd", "steps", "tokens_in", "tokens_out"])
        for r in sorted(runs, key=lambda r: (_mord(r["model"]), r["task"], r["run"])):
            m = man.get(r["label"], {})
            w.writerow([r["task"], r["model"], r["run"], int(r["passed"]),
                        m.get("start", ""), m.get("end", ""), m.get("duration_s", ""),
                        round(r["elapsed"], 1), round(r["generation"], 1),
                        "" if r["cost"] is None else round(r["cost"], 4),
                        "" if r["sole"] is None else round(r["sole"], 4),
                        r["steps"], r["tin"], r["tout"]])
    sole_tot = defaultdict(float)
    for r in runs:
        if r["sole"] is not None:
            sole_tot[r["model"]] += r["sole"]
    sm_path = os.path.join(run_dir, "summary.csv")
    with open(sm_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["setup", "runs", "pass_runs", "run_pass_rate_pct", "tasks_solved", "tasks_total",
                    "task_pass_rate_pct", "total_usd", "avg_usd_per_run", "usd_per_pass_run",
                    "usd_per_task_solved", "total_sole_usd", "cost_basis"])
        for m in MODEL_ORDER + [x for x in run_stat if x not in MODEL_ORDER]:
            if m not in run_stat:
                continue
            rs = run_stat[m]
            n, pr = rs["runs"], rs["pass"]
            solved_t, tot_t = per_model[m]["pass"], per_model[m]["pass"] + per_model[m]["fail"]
            tot = model_cost.get(m, 0.0)
            basis = "claude_code" if m == "opus" else ("real" if cost_is_real else "modeled")
            w.writerow([m, n, pr, round(100 * pr / n, 1) if n else "",
                        solved_t, tot_t, round(100 * solved_t / tot_t, 1) if tot_t else "",
                        round(tot, 4), round(tot / n, 4) if n else "",
                        round(tot / pr, 4) if pr else "", round(tot / solved_t, 4) if solved_t else "",
                        round(sole_tot.get(m, 0.0), 4), basis])
    _step(4, STEPS, "write csv")

    # ---- concurrency: how many runs overlapped in time (packing on the shared box/endpoint) ----
    ivals = []
    mpath = os.path.join(run_dir, "manifest.csv")
    if os.path.exists(mpath):
        with open(mpath) as mf:
            for row in csv.DictReader(mf):
                try:
                    ivals.append((float(row["start"]), float(row["end"])))
                except (TypeError, ValueError, KeyError):
                    pass
    peak_c = peak_concurrency(ivals) if ivals else 0
    avg_c = round(avg_concurrency(ivals), 1) if ivals else 0.0

    # ---- takeaway: cheapest GLM tier per solved task vs Opus (factual, from the numbers above) ----
    def _stat(m):
        s = per_model.get(m, {"pass": 0, "fail": 0})
        n = s["pass"] + s["fail"]
        return {"rate": (100 * s["pass"] / n) if n else 0.0,
                "ps": (model_cost.get(m, 0.0) / s["pass"]) if s["pass"] else None}
    _glm = {m: _stat(m) for m in ("glm-default", "glm-high", "glm-nothink") if m in per_model}
    _best = min((m for m in _glm if _glm[m]["ps"] is not None), key=lambda m: _glm[m]["ps"], default=None)
    takeaway = ""
    if _best:
        g, o = _glm[_best], _stat("opus")
        takeaway = (f"Cheapest per solved task: {MODEL_LABEL.get(_best, _best)} at "
                    f"${g['ps']:.2f}/solve ({g['rate']:.0f}% of tasks solved)")
        if o["ps"] and g["ps"]:
            ratio = o["ps"] / g["ps"]                       # >1 => GLM cheaper than Opus
            if ratio >= 1:
                rel = f"{ratio:.1f}× cheaper" if ratio >= 2 else f"{(ratio - 1) * 100:.0f}% cheaper"
            else:
                inv = 1 / ratio
                rel = f"{inv:.1f}× pricier" if inv >= 2 else f"{(inv - 1) * 100:.0f}% pricier"
            takeaway += f" — {rel} than {MODEL_LABEL['opus']} (${o['ps']:.2f}/solve, {o['rate']:.0f}%)."
        else:
            takeaway += "."

    # ---- HTML report ----
    out = os.path.join(run_dir, "progress_report.html")
    write_html(out, run_dir, runs, rows, comp, cell, cost_is_real, peak_c, avg_c, takeaway, run_rows)
    _step(5, STEPS, f"html → {out}")
    if sys.stdout.isatty():
        import webbrowser
        webbrowser.open(Path(out).resolve().as_uri())



if __name__ == "__main__":
    main()
