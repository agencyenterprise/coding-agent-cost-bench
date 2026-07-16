#!/usr/bin/env python3
"""
Interim benchmark progress report — safe to run mid-run, reports on whatever runs
exist so far (not a final scorecard).

Per-model pass/fail table (like the report image) + a per-task complexity table,
built straight from a run folder's output.log + reward.json files.

Pass/fail comes from each run's reward.json (reward == 1 => pass). Effort signals
(steps, tool calls, output tokens, duration) come from output.log and feed
aggregate.task_complexity — the SAME min-max-normalized 0-10 score used elsewhere.

Two tables:
  * Per-model: Pass / Fail / Pass Rate, grouped by TASK — a task counts as a pass if the
    model solved it in at least one run (pass@k over its runs), not per individual run.
  * Per-task:  Complexity + Pass / Fail / Pass Rate pooled across all models.

Usage: python3 model_complexity_report.py [RUN_DIR]
  RUN_DIR defaults to the newest folder under .docs/results/.
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import aggregate  # reuse claude_stats / log_stats / task_complexity

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
    return aggregate.GPU_HOURLY_USD, False


def collect(run_dir):
    """Walk run_dir's top-level run folders -> per-run records, pooled task_stat, and a
    per-(task, model) [passes, total] cell map for the matrix."""
    runs = []
    # task_stat shape expected by aggregate.task_complexity, keyed by task here (no prompt dim)
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
        pm = aggregate.claude_stats(rundir) if is_claude else aggregate.log_stats(rundir)
        dur = run_duration_s(rundir, is_claude)
        # Claude reports its own $ (None if the run was killed before its result event);
        # GLM is self-hosted -> $ = API-call seconds x GPU rate.
        cost = pm.get("cost") if is_claude else (pm["call_s"] / 3600.0 * rate_hr)
        passed = is_pass(rundir)
        runs.append({"model": model, "task": task, "passed": passed})
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


def _table(headers, rows, aligns):
    """Minimal fixed-width text table. aligns: 'l' or 'r' per column."""
    cols = list(zip(*([headers] + rows))) if rows else [[h] for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]

    def fmt(cells):
        return "  ".join(
            str(c).ljust(w) if al == "l" else str(c).rjust(w)
            for c, w, al in zip(cells, widths, aligns))
    line = "-" * (sum(widths) + 2 * (len(widths) - 1))
    print(fmt(headers))
    print(line)
    for r in rows:
        print(fmt(r))


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
footer{color:var(--muted);font-size:12px;margin-top:40px}
"""


def _cell_cls(passes, total):
    if total == 0:
        return "z", "·"
    r = passes / total
    cls = "g" if r >= 0.75 else "a" if r >= 0.45 else "r"
    return cls, f"{passes}/{total}"


def write_html(path, run_dir, runs, model_rows, comp, cell):
    """Self-contained report.html — the same three tables as the chat visualization."""
    import html as _h
    esc = lambda x: _h.escape(str(x))
    models = [m for m in MODEL_ORDER if any(k[1] == m for k in cell)]
    tasks_by_comp = sorted(comp, key=lambda t: -comp[t]["complexity"])

    # per-model rows
    mbody = ""
    for label, p, f, rate in model_rows:
        frac = (p / (p + f)) if (p + f) else 0
        mbody += (f"<tr><td><b>{esc(label)}</b></td><td class=num>{p}</td><td class=num>{f}</td>"
                  f"<td class=num>{esc(rate)}<span class=bar><i style='width:{frac*100:.0f}%'></i></span></td></tr>")

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
<p class=sub>{esc(run_dir)} · {len(runs)} runs so far · {len(comp)} tasks</p>

<h2>Per model — tasks solved (passed in ≥1 run) / unsolved</h2>
<div class=tw><table><thead><tr><th>Model</th><th class=num>Pass</th><th class=num>Fail</th>
<th class=num>Pass rate</th></tr></thead><tbody>{mbody}</tbody></table></div>

<h2>Per task — complexity (0–10, hardest first) and pass / fail across all models</h2>
<div class=tw><table><thead><tr><th>Task</th><th class=num>Complexity</th><th class=num>Pass</th>
<th class=num>Fail</th><th class=num>Pass rate</th></tr></thead><tbody>{tbody}</tbody></table></div>

<h2>Passes per task × model</h2>
<div class=tw><table><thead><tr><th>Task</th><th class=num>Complexity</th>{mx_head}</tr></thead>
<tbody>{mx_body}</tbody></table></div>
<div class=legend><span><span class="sw g"></span>≥75% pass</span>
<span><span class="sw a"></span>45–75%</span><span><span class="sw r"></span>&lt;45%</span>
<span><span class="sw" style="background:var(--border)"></span>no runs</span></div>

<footer>Complexity: min-max-normalized effort (steps, tools, output tokens, duration) pooled across
all runs, scaled 0–10 — relative to this task set. Interim progress report (benchmark may
still be running). Generated by benchmark_progress_report.py.</footer>
</div></body></html>"""
    with open(path, "w") as f:
        f.write(doc)
    return path


def main():
    default = None
    base = Path("results")
    if base.is_dir():
        subs = sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)
        default = str(subs[0]) if subs else None
    run_dir = sys.argv[1] if len(sys.argv) > 1 else default
    if not run_dir or not os.path.isdir(run_dir):
        sys.exit(f"run dir not found: {run_dir!r} — pass one as an argument")

    runs, task_stat, cell, cost_is_real = collect(run_dir)
    if not runs:
        sys.exit(f"no run folders (with reward.json) under {run_dir}")

    comp = aggregate.task_complexity(task_stat)   # {task: {complexity, pass_rate, ...}}

    print(f"\nRun: {run_dir}   ({len(runs)} runs, {len(task_stat)} tasks)\n")

    # ---- per-model table: pass@k — a TASK counts as pass if the model solved it in ANY of its runs
    # (fail,fail,fail,pass -> pass; fail,fail,fail -> fail). So Pass/Fail count tasks, not runs. ----
    solved = defaultdict(bool)   # (model, task) -> solved in at least one run
    for r in runs:
        solved[(r["model"], r["task"])] |= bool(r["passed"])
    per_model = defaultdict(lambda: {"pass": 0, "fail": 0})
    for (model, _task), ok in solved.items():
        per_model[model]["pass" if ok else "fail"] += 1
    rows = []
    for model in MODEL_ORDER + [m for m in per_model if m not in MODEL_ORDER]:
        if model not in per_model:
            continue
        s = per_model[model]
        tot = s["pass"] + s["fail"]
        rows.append([
            MODEL_LABEL.get(model, model), s["pass"], s["fail"],
            f"{100 * s['pass'] / tot:.1f}%" if tot else "-",
        ])
    _table(["Model", "Pass", "Fail", "Pass Rate"], rows, ["l", "r", "r", "r"])

    # ---- per-task table (hardest first) ----
    print()
    trows = []
    for task in sorted(comp, key=lambda t: -comp[t]["complexity"]):
        c = comp[task]
        p = int(round(c["pass_rate"] * c["runs"]))
        trows.append([task, f"{c['complexity']:.1f}", p, c["runs"] - p,
                      f"{100 * c['pass_rate']:.1f}%"])
    _table(["Task", "Complexity", "Pass", "Fail", "Pass Rate"], trows,
           ["l", "r", "r", "r", "r"])
    print()

    # ---- deepswe_task_difficulty.csv (into the run folder), hardest first ----
    import csv
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
    _basis = "real (billing.json)" if cost_is_real else f"modeled {aggregate.GPU_HOURLY_USD:.1f}/hr"
    print(f"csv:    {csv_path}  (avg_cost_usd basis: {_basis})")

    # ---- HTML report (its own filename so it never clobbers aggregate.py's report.html) ----
    out = os.path.join(run_dir, "progress_report.html")
    write_html(out, run_dir, runs, rows, comp, cell)
    print(f"report: {out}")   # progress_report.html — separate from aggregate.py's report.html
    if sys.stdout.isatty():
        import webbrowser
        webbrowser.open(Path(out).resolve().as_uri())


if __name__ == "__main__":
    main()
