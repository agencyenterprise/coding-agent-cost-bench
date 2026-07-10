#!/usr/bin/env python3
"""
Aggregate benchmark results -> results/{results_detailed,summary}.csv.

ONE cost per row, always labeled with its basis. Two currencies:
  * API models (Claude/GPT/Gemini) -> cost = tokens x per-token price (how the provider
    bills). From ccusage. basis "api_ccusage".
  * GLM on Modal (self-host)        -> cost = per-run sum of API-call seconds (step_start ->
    step_finish in output.log) x hourly rate. basis "gpu_calls". Local script/tool gaps
    (pip/pytest/git/bash between steps) are excluded. cost_per_successful_task = mean of that
    per-run API cost over passes only (not job wall-clock, not pooled across concurrent tasks).
    gen_s in summary is still the union of API windows (utilization); dollars use summed call_s.
    Modal sole-tenant uptime billing (active_s) is reported separately as idle tax.

Inputs: results/manifest.csv + results/<outdir>/usage.json (ccusage session --json shape).
Self-host detection: any model served via the `modal/` provider (e.g. modal/zai-org/GLM-5.2-FP8).
"""
import csv
import json
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Defaults; overridden by --results-dir / --rate in main(). (judge.py imports this module and sets
# these two attributes directly, so they stay module-level rather than env-driven.)
RESULTS_DIR = "./results"

# Hourly run-rate of the 8xB200 auto-endpoint while it's actively serving. Measured from this
# workspace's own Modal billing ($32.08 over the 38 active minutes of the first run = $50.7/hr,
# i.e. ~$6.34 per B200-hour x 8). Override per hardware tier with --rate.
GPU_HOURLY_USD = 50.7


def is_self_hosted(model):
    """Self-hosted (GPU-billed) = served via a modal provider (modal/ or modal-nothink/)."""
    return model.lower().split("/")[0].startswith("modal")


def harness_of(ref):
    """Which agent harness ran this model ref: Claude Code's CLI vs opencode."""
    return "claude-code" if ref.startswith("claude-code/") else "opencode"


def model_id(ref):
    """The model itself, without the harness/provider prefix (e.g. claude-opus-4-8)."""
    return ref.split("/", 1)[1] if "/" in ref else ref


def harness_disp(h):
    """Human label for the harness column."""
    return "claude-code" if h == "claude" else h


def model_disp(ref):
    """Display id that KEEPS the provider so variants stay distinct, e.g. modal/GLM-5.2-FP8 vs
    modal-nothink/GLM-5.2-FP8 (thinking on vs off) — dropping only the org path."""
    p = ref.split("/")
    return f"{p[0]}/{p[-1]}" if len(p) > 1 else ref


def task_source(name, tasks_dir="tasks"):
    """Prompt provenance: 'swe-bench' if the task carries SWE-bench dataset artifacts (a test.patch),
    else 'invented' (our injected-bug demos + the from-scratch build task). NOTE: swe-bench prompts
    embed the real problem statement verbatim but are still wrapped in our uniform v2 template."""
    if os.path.exists(os.path.join(tasks_dir, name, "test.patch")):
        return "swe-bench"
    return "swe-bench" if "swebench" in name.lower() else "invented"


def _pred_key(harness, model, prompt, run):
    """Reconstruct make_predictions.py's model_name_or_path for one run, so a SWE task's pass/fail
    can be looked up in resolved.json (the official Modal Docker grade)."""
    safe = model.replace("/", "_").replace(":", "_").replace(" ", "_")
    return f"{harness}__{safe}__{prompt}__run{run}"


def load_usage(outdir):
    """(tokens_in, tokens_out, ccusage_cost) for a run from its usage.json.

    tokens_in is the TOTAL input the model processed = fresh input + cache-creation +
    cache-read. ccusage splits these out, and heavy cachers (Claude) put almost everything in
    cacheRead — so summing only `inputTokens` shows a misleading ~0. Summing all three makes
    the input comparable across providers (cost is unaffected: it comes from ccusage totalCost,
    which already prices cached tokens cheaper)."""
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
    """Parse output.log (opencode JSON-lines) once into work/efficiency metrics for a run:
      call_s  - endpoint generation time: sum over steps of (step_finish - step_start), i.e. the FULL
                request->response wall-clock per model call INCLUDING token streaming (not just
                time-to-first-token), excluding local tool/script gaps between steps (pip/pytest/git).
      steps   - assistant turns; tools - tool calls; prose - chars of natural-language text;
      reason  - reasoning tokens; out - output tokens (tool args + prose the model emitted)."""
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
            if start is not None and ts is not None:     # full step: request start -> response complete
                call_ms += ts - start
            start = None
    m["call_s"] = call_ms / 1000.0
    return m


def claude_stats(outdir):
    """Parse a Claude Code run's stream-json output.log: native cost/usage/turns (from the final
    `result` event) plus tool-call and prose counts (from `assistant` events). Claude Code reports
    everything itself, so no ccusage needed. tin includes cache tokens (fair vs the others)."""
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
            if ev.get("num_turns"):
                m["steps"] = ev["num_turns"]
    return m


def gen_intervals(outdir):
    """Absolute-epoch (start, end) windows the model spent GENERATING, per step (step_start ->
    step_finish = the full request->response, INCLUDING token streaming — not just time-to-first-
    token), from an opencode output.log. Unioning these across concurrent runs gives concurrency-
    correct generation wall-clock — so idle = uptime_union - gen_union is always >= 0 (unlike
    active_s - sum(call_s), which breaks when parallel runs generate at the same time)."""
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
            out.append((start / 1000.0, ts / 1000.0))     # full step wall-clock (ms epoch -> s)
            start = None
    return out


def union_seconds(intervals):
    """Wall-clock seconds the endpoint was actively serving = union of (start, end) run
    intervals. Overlapping/parallel runs are merged, so 3 runs sharing a 3-min window count
    as 3 min, not 9 — you pay for the GPU being up, not per concurrent request."""
    total = 0.0
    cur_s = cur_e = None
    for s, e in sorted(intervals):
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:                 # overlaps the current merged interval -> extend it
            cur_e = max(cur_e, e)
        else:                            # gap -> close the current interval, start a new one
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    if cur_s is not None:
        total += cur_e - cur_s
    return total


def task_complexity(task_stat):
    """Empirical, RELATIVE complexity per (task, prompt version) from observed effort pooled across
    all runs/models of that pair: mean steps, tool calls, output tokens, and duration — each min-max
    normalized across the (task, version) set, averaged, scaled to 0-10. Higher = demanded more work.
    Relative to this set, not absolute. pass_rate is reported alongside (outcome, kept separate)."""
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


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _iso(ts):
    """Epoch seconds -> local ISO 'YYYY-MM-DDTHH:MM:SS' (matches the Modal dashboard clock)."""
    return "" if ts is None else datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def gpu_call_cost(call_s):
    """USD for GLM self-host: API-call seconds only (excludes local tool/script time)."""
    return call_s / 3600 * GPU_HOURLY_USD if call_s else 0.0


def resolve_outdir(outdir):
    """manifest.csv stores outdir as an absolute path under whatever RESULTS_DIR wrote it (e.g.
    /app/results/... from inside Docker). Re-anchor to the current RESULTS_DIR so reading the
    manifest from a different mount (e.g. locally against the bind-mounted results/ folder) doesn't
    silently return empty stats for every run — every stats function keys off this failing open()."""
    return os.path.join(RESULTS_DIR, os.path.basename(outdir)) if outdir else outdir


def _report_file_url(path):
    return Path(path).resolve().as_uri()


def _terminal_link(text, url):
    """OSC 8 hyperlink — clickable in Cursor/VS Code/iTerm/etc."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def _open_report(path):
    url = _report_file_url(path)
    if sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    else:
        import webbrowser
        webbrowser.open(url)


def _emit_report_ready(path, *, open_browser=False):
    abs_path = str(Path(path).resolve())
    url = _report_file_url(abs_path)
    print(f"✓ report: {_terminal_link(abs_path, url)}")
    if open_browser:
        _open_report(abs_path)


def main():
    import argparse
    global RESULTS_DIR, GPU_HOURLY_USD
    ap = argparse.ArgumentParser(description="Aggregate benchmark results into summary.csv.")
    ap.add_argument("--results-dir", default=RESULTS_DIR, help="results dir to read/write [%(default)s]")
    ap.add_argument("--rate", type=float, default=GPU_HOURLY_USD,
                    help="GLM GPU $/hr for this hardware tier [%(default)s]")
    ap.add_argument("--open", action=argparse.BooleanOptionalAction, default=None,
                    help="open report.html in the default browser "
                         "(default: on macOS in an interactive terminal)")
    args = ap.parse_args()
    RESULTS_DIR, GPU_HOURLY_USD = args.results_dir, args.rate
    open_report = args.open
    if open_report is None:
        open_report = (
            sys.platform == "darwin"
            and sys.stdout.isatty()
            and not os.environ.get("CI")
        )

    manifest = os.path.join(RESULTS_DIR, "manifest.csv")
    if not os.path.exists(manifest):
        sys.exit(f"no manifest at {manifest} — run bench.sh first "
                 f"(or point --results-dir at the right folder, e.g. --results-dir results/app-8xH200)")

    # Official SWE-bench grade (from grade_swe.sh / swe_eval_modal.py). If present, it overrides the
    # host verify.sh pass/fail for SWE tasks — Docker-in-the-right-Python is the trustworthy signal.
    resolved_map = {}
    _rp = os.path.join(RESULTS_DIR, "resolved.json")
    if os.path.exists(_rp):
        try:
            resolved_map = json.load(open(_rp))
        except Exception:
            resolved_map = {}

    detailed = []
    # arm = (harness, model, prompt-version): the prompt version is a first-class sweep dimension,
    # so v1 vs v2 of the SAME (harness, model) are separate rows and never pooled together.
    intervals = defaultdict(list)   # arm -> [(start, end), ...] run intervals (uptime)
    genivs = defaultdict(list)      # arm -> [(start, end), ...] generation windows (concurrency-safe)
    eff = defaultdict(lambda: {"steps": 0, "tools": 0, "prose": 0, "reason": 0, "out": 0})
    task_stat = defaultdict(lambda: {"runs": 0, "steps": 0, "tools": 0, "out": 0, "dur": 0.0, "pass": 0, "api_cost": 0.0})
    with open(manifest) as f:
        for row in csv.DictReader(f):
            m = row.get("model")
            if not m or not row.get("task") or not row.get("outdir"):
                continue   # skip malformed/partial lines (e.g. a stray write that corrupted the manifest)
            row["outdir"] = resolve_outdir(row["outdir"])
            h = row.get("harness") or harness_of(m)   # harness recorded by bench.sh (fallback for old data)
            if h == "deepclaude":
                continue   # deepclaude harness removed — drop any lingering rows from older runs
            if h == "opencode" and not is_self_hosted(m):
                continue   # Opus·opencode arm dropped — opencode now drives only the GLM (modal*) arms
            pv = row.get("prompt") or "v1"             # prompt version (fallback for pre-sweep manifests)
            if resolved_map and "swebench" in row["task"].lower():
                iid = row["task"].split("demo-swebench-", 1)[-1]      # task dir -> SWE instance id
                rk = f"{iid}::{_pred_key(h, m, pv, row.get('run'))}"  # composite: same mnp recurs per instance
                if rk in resolved_map:                 # official Docker grade wins over host verify
                    row["status"] = "pass" if resolved_map[rk].get("resolved") else "fail"
            key = (h, m, pv)
            s, e = _f(row.get("start")), _f(row.get("end"))
            if s is not None and e is not None:
                intervals[key].append((s, e))
            if h == "claude":                          # parses Claude Code's stream-json output.log
                pm = claude_stats(row.get("outdir", ""))
                tin, tout, ccost, cs = pm["tin"], pm["tout"], pm["cost"], pm["call_s"]
                basis = "claude_code"                   # Anthropic model, Claude Code reports its own $
            else:
                tin, tout, ccost = load_usage(row.get("outdir", ""))
                pm = log_stats(row.get("outdir", ""))  # work/efficiency metrics from opencode log
                if is_self_hosted(m):
                    genivs[key] += gen_intervals(row.get("outdir", ""))   # absolute gen windows to union
                cs = pm["call_s"]
                basis = "gpu_calls" if is_self_hosted(m) else "api_ccusage"
            for k in eff[key]:
                eff[key][k] += pm[k]
            ts = task_stat[(row["task"], pv)]           # empirical complexity per (task, prompt version)
            ts["runs"] += 1
            ts["steps"] += pm["steps"]; ts["tools"] += pm["tools"]; ts["out"] += pm["out"]
            ts["dur"] += _f(row.get("duration_s")) or 0.0
            ts["pass"] += 1 if row["status"] == "pass" else 0
            if is_self_hosted(m):
                ts["api_cost"] += gpu_call_cost(cs)
            run_cost = round(gpu_call_cost(cs), 4) if is_self_hosted(m) else round(ccost, 6)
            detailed.append({
                "task": row["task"], "harness": h, "model": m, "prompt": pv, "run": row["run"],
                "status": row["status"],
                "start": _iso(s), "end": _iso(e), "duration_s": row.get("duration_s", ""),
                "call_s": round(cs, 2),
                "tokens_in": tin, "tokens_out": tout,
                "cost_usd": run_cost if (run_cost or not is_self_hosted(m)) else "",
                "cost_basis": basis,
            })
    if not detailed:
        sys.exit("manifest has no rows")

    with open(os.path.join(RESULTS_DIR, "results_detailed.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(detailed[0].keys()))
        w.writeheader()
        w.writerows(detailed)

    by_arm = defaultdict(list)
    for d in detailed:
        by_arm[(d["harness"], d["model"], d["prompt"])].append(d)

    rows = []
    for (harness, model, prompt), runs in by_arm.items():
        key = (harness, model, prompt)
        n = len(runs)
        passes = sum(1 for r in runs if r["status"] == "pass")
        durs = [_f(r["duration_s"]) for r in runs if _f(r["duration_s"]) is not None]
        active = union_seconds(intervals[key])                        # wall-clock union = sole-tenant uptime
        overlap = sum(e - s for s, e in intervals[key]) - active      # time compressed by parallelism
        call_total = sum(v for r in runs if (v := _f(r.get("call_s"))) is not None)   # Σ API-call s
        gen_wall = union_seconds(genivs[key])                         # API wall-clock union (parallel-safe)
        idle = max(0.0, active - gen_wall)                            # up but not generating anything (>= 0)
        pass_call_s = [_f(r["call_s"]) for r in runs if r["status"] == "pass" and _f(r["call_s"]) is not None]
        row = {
            "harness": harness, "model": model, "prompt": prompt, "runs": n, "passes": passes,
            "success_rate": round(passes / n, 3),
            "avg_tokens_in": round(statistics.mean(r["tokens_in"] for r in runs)),
            "avg_tokens_out": round(statistics.mean(r["tokens_out"] for r in runs)),
            "avg_duration_s": round(statistics.mean(durs), 1) if durs else "",
            "avg_call_s_pass": round(statistics.mean(pass_call_s), 1) if pass_call_s else "",
            "call_s": round(call_total, 1) if call_total else "",     # Σ API-call seconds (all runs)
            "gen_s": round(gen_wall, 1) if gen_wall else "",          # API wall-clock union (utilization)
            "active_s": round(active, 1) if active else "",
            "idle_s": round(idle, 1) if (intervals[key] and gen_wall) else "",
            "overlap_s": round(overlap, 1) if intervals[key] else "",
            "total_cost_usd": "", "gpu_wall_cost_usd": "", "cost_per_successful_task": "", "cost_basis": "",
        }
        if is_self_hosted(model):
            # Dollars from API-call seconds only (step_start→step_finish per step; no local tools).
            # cost_per_successful_task = mean API $ over passes. total_cost_usd = Σ all runs (incl. fails).
            # gpu_wall_cost_usd = gen_s union × rate = shared-endpoint wall-clock bill when parallel.
            if pass_call_s or call_total:
                row["total_cost_usd"] = round(gpu_call_cost(call_total), 4) if call_total else ""
                row["gpu_wall_cost_usd"] = round(gpu_call_cost(gen_wall), 4) if gen_wall else ""
                row["cost_per_successful_task"] = (
                    round(gpu_call_cost(statistics.mean(pass_call_s)), 4) if pass_call_s else "")
                row["cost_basis"] = "gpu_calls"
            else:
                row["cost_basis"] = "gpu_calls (no log timing)"
        else:   # token-billed (opencode APIs) or Claude Code (reports its own $)
            total = sum(r["cost_usd"] for r in runs if r["cost_usd"] != "")
            row["total_cost_usd"] = round(total, 4)
            row["cost_per_successful_task"] = round(total / passes, 4) if passes else ""
            row["cost_basis"] = runs[0]["cost_basis"]
        rows.append(row)

    # order rows so each arm's prompt versions sit together (v1 before v2), arms in matrix order
    arm_order = {}
    for d in detailed:
        arm_order.setdefault((d["harness"], d["model"]), len(arm_order))
    rows.sort(key=lambda r: (arm_order[(r["harness"], r["model"])], r["prompt"]))

    with open(os.path.join(RESULTS_DIR, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # write complexity.csv (data), then render the console report
    comp = task_complexity(task_stat)   # keyed by (task, prompt version)
    # group each task's versions together (v1 -> v2 -> v3), tasks ranked by their hardest version
    peak = {}
    for (t, pv), a in comp.items():
        peak[t] = max(peak.get(t, 0.0), a["complexity"])
    crows = [{"task": t[0], "prompt": t[1], "source": task_source(t[0]), "complexity": a["complexity"],
              "pass_rate": a["pass_rate"], "runs": a["runs"],
              "avg_steps": round(a["avg_steps"], 1), "avg_tools": round(a["avg_tools"], 1),
              "avg_out_tok": round(a["avg_out"]), "avg_dur_s": round(a["avg_dur"], 1),
              "avg_api_cost_usd": round(task_stat[t]["api_cost"] / max(a["runs"], 1), 4)}
             for t, a in sorted(comp.items(), key=lambda kv: (-peak[kv[0][0]], kv[0][0], kv[0][1]))]
    if crows:
        with open(os.path.join(RESULTS_DIR, "complexity.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(crows[0].keys()))
            w.writeheader()
            w.writerows(crows)
    arows = arm_rollup(rows, genivs)
    all_gen = [iv for k, v in genivs.items() if is_self_hosted(k[1]) for iv in v]
    overall_peak = peak_concurrency(all_gen) if all_gen else None
    if arows:
        with open(os.path.join(RESULTS_DIR, "arms.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(arows[0].keys()))
            w.writeheader()
            w.writerows(arows)
    if rows:
        path = _html_report(RESULTS_DIR, rows, arows, eff, crows, overall_peak, detailed)
        _emit_report_ready(path, open_browser=open_report)
        print(f"  data:   {RESULTS_DIR}/summary.csv  {RESULTS_DIR}/arms.csv  {RESULTS_DIR}/complexity.csv  {RESULTS_DIR}/results_detailed.csv")


def _mname(model):
    """Friendly model name (no harness), e.g. GLM-5.2 FP8 (no thinking) / Claude Opus 4.8.
    Harness is shown in its own column, so it's not folded in here."""
    prov, last = model.split("/")[0], model.split("/")[-1]
    name = {"claude-opus-4-8": "Claude Opus 4.8"}.get(last, last.replace("-FP8", " FP8"))
    tag = {"modal": " (max reasoning)", "modal-nothink": " (no thinking)",
           "modal-high": " (high reasoning)"}.get(prov, "")
    return name + tag


def _tshort(t):
    t = t.replace("demo-swebench-", "").replace("demo-", "")
    return t.split("__")[-1] if "__" in t else t


def _arm_label(harness, model):
    """Arm name for rollup tables: full model name (incl. reasoning tier) for GLM; '<model> · <harness>' for API."""
    if is_self_hosted(model):
        return _mname(model)
    return f"{_mname(model)} · {harness_disp(harness)}"


def peak_concurrency(ivals):
    """Max number of generation windows open at the same instant (sweep line over start/end
    events). Touching intervals (one ends exactly as the next starts) do NOT count as overlapping."""
    ev = []
    for s, e in ivals:
        if s is None or e is None or e < s:
            continue
        ev.append((s, 1)); ev.append((e, -1))
    ev.sort(key=lambda x: (x[0], x[1]))     # at equal timestamps process ends (-1) before starts (+1)
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


def packing_factor(ivals):
    """Effective parallelism = Σ step durations ÷ union(gen wall-clock)."""
    total = sum(e - s for s, e in ivals if s is not None and e is not None and e >= s)
    wall = union_seconds(ivals)
    return total / wall if wall else 0.0


def concurrency_scenarios(sole, avg, peak, packing, packed):
    """$/task ladder: sole → projected at lower conc → measured packed at observed packing."""
    if sole in ("", None):
        return []
    sole = float(sole)
    out = [{"label": "Sole (1 task)", "conc": 1, "cost": sole, "basis": "measured"}]
    if avg:
        avg = float(avg)
        half = avg / 2
        out.append({"label": "Half avg", "conc": round(half, 1),
                    "cost": round(sole / half, 4), "basis": "projected"})
        out.append({"label": "Avg observed", "conc": round(avg, 1),
                    "cost": round(sole / avg, 4), "basis": "projected"})
    if peak not in ("", None):
        pk = float(peak)
        out.append({"label": "Peak instant (spike)", "conc": pk,
                    "cost": round(sole / pk, 4), "basis": "projected"})
    if packing and packed not in ("", None):
        out.append({"label": "Packed (this run)", "conc": round(float(packing), 1),
                    "cost": packed, "basis": "measured",
                    "peak": round(float(peak), 1) if peak not in ("", None) else ""})
    return out


def family_rollup(rows):
    """One row per reasoning arm — GLM broken out by default / high / no-think (NOT pooled), each API
    model separate — pooled passes/runs per arm. The headline per-variant success comparison."""
    groups, order = {}, []
    for r in rows:
        label = _arm_label(r["harness"], r["model"])
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(r)
    out = []
    for label in order:
        g = groups[label]
        runs = sum(int(r["runs"]) for r in g)
        passes = sum(int(r["passes"]) for r in g)
        out.append({"family": label, "runs": runs, "passes": passes,
                    "success_rate": round(passes / runs, 3) if runs else ""})
    return out


def arm_rollup(rows, genivs=None):
    """Collapse the per-(arm, prompt) summary rows into one row per arm (harness+model), pooling
    across prompt versions. Success is a min–max range over the versions; costs are passes-weighted
    so they match the per-prompt 'Cost per Completed Task' table. If genivs (per-(h,m,prompt) list of
    absolute gen windows) is passed, also report peak concurrent generation requests. Order follows `rows`."""
    groups, order = {}, []
    for r in rows:
        k = (r["harness"], r["model"])
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    out = []
    for (harness, model) in order:
        g = groups[(harness, model)]
        sh = is_self_hosted(model)
        peak = avg_c = packing = ""
        if genivs is not None and sh:
            ivs = [iv for k, v in genivs.items() if k[0] == harness and k[1] == model for iv in v]
            if ivs:
                peak = peak_concurrency(ivs)
                avg_c = round(avg_concurrency(ivs), 1)
                packing = round(packing_factor(ivs), 1)
        runs = sum(int(r["runs"]) for r in g)
        passes = sum(int(r["passes"]) for r in g)
        srates = [float(r["success_rate"]) for r in g if r.get("success_rate") not in ("", None)]
        gpu_s = sum(_f(r.get("call_s")) or 0 for r in g)                          # Σ generation seconds
        out_tok = sum((_f(r.get("avg_tokens_out")) or 0) * int(r["runs"]) for r in g)
        sole_num = sum((_f(r.get("cost_per_successful_task")) or 0) * int(r["passes"]) for r in g)
        wall_total = sum(_f(r.get("gpu_wall_cost_usd")) or 0 for r in g)          # packed = wall ÷ passes
        sole = round(sole_num / passes, 4) if passes else ""
        out.append({
            "arm": _arm_label(harness, model), "harness": harness, "model": model,
            "runs": runs, "passes": passes,
            "success_min": round(min(srates), 3) if srates else "",
            "success_max": round(max(srates), 3) if srates else "",
            "success_pooled": round(passes / runs, 3) if runs else "",
            "avg_tokens_out": round(out_tok / runs) if runs else 0,
            "gpu_s_per_run": round(gpu_s / runs, 1) if (sh and gpu_s and runs) else "",
            "peak_concurrency": peak,
            "avg_concurrency": avg_c,
            "packing_factor": packing,
            "cost_sole_per_task": sole,
            # API models are per-token (concurrency-invariant), so packed == sole for them.
            "cost_packed_per_task": (round(wall_total / passes, 4) if (sh and passes) else sole),
        })
    return out


_HTML_CSS = """
:root{--bg:#f6f7f9;--fg:#16181d;--muted:#6b7280;--card:#fff;--border:#e5e7eb;--head:#f9fafb;
 --accent:#4f46e5;--good:#16a34a;--bar:#22c55e;--best:#ecfdf5;--best-br:#34d399;
 --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#0e1014;--fg:#e6e8ec;--muted:#9aa1ad;--card:#161a21;
 --border:#282d38;--head:#1b2028;--accent:#8b93f8;--good:#34d399;--bar:#22c55e;--best:#0d2a1e;--best-br:#15803d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1060px;margin:0 auto;padding:34px 20px 90px}
h1{font-size:25px;margin:0 0 3px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 22px;font-size:13.5px}
.chips{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 10px}
.chip{background:var(--card);border:1px solid var(--border);border-radius:11px;padding:9px 15px;min-width:96px}
.chip b{display:block;font-size:19px;font-family:var(--mono)}
.chip span{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
h2{font-size:15px;margin:34px 0 5px;letter-spacing:.01em}
.note{color:var(--muted);font-size:12.5px;margin:2px 0 13px;max-width:760px}
.tw{overflow-x:auto;border:1px solid var(--border);border-radius:13px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:9px 15px;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap}
thead th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600;background:var(--head)}
tbody tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-family:var(--mono)}
tr.best{background:var(--best)}tr.best td:first-child{box-shadow:inset 3px 0 0 var(--best-br)}
.bar{background:var(--border);border-radius:5px;height:7px;width:104px;display:inline-block;vertical-align:middle;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--bar);border-radius:5px}
.pill{font-family:var(--mono);font-size:12.5px;margin-left:8px}
.mut{color:var(--muted);font-size:12px;margin-left:6px}
.tag{font-size:11px;color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:1px 6px}
footer{color:var(--muted);font-size:12px;margin-top:44px}
a{color:var(--accent)}
"""


def _html_report(results_dir, rows, arms, eff, crows, overall_peak=None, detailed=None):
    """Write a self-contained report.html (inline CSS, light/dark aware) next to the CSVs."""
    import html as _h
    from datetime import datetime
    esc = lambda x: _h.escape(str(x))
    usd = lambda v: f"${v:.3f}" if v not in ("", None) else "—"

    def packed(r):
        if is_self_hosted(r["model"]):
            w, p = _f(r.get("gpu_wall_cost_usd")), r.get("passes")
            return w / p if (w and p) else ""
        return r.get("cost_per_successful_task")

    def rng(a):
        if a["success_min"] == "":
            return "—"
        lo, hi = a["success_min"], a["success_max"]
        return f"{lo:.0%}" if lo == hi else f"{lo:.0%}–{hi:.0%}"

    def sbar(frac, label, extra=""):
        f = 0 if frac in ("", None) else float(frac)
        return (f'<span class="bar"><i style="width:{f*100:.0f}%"></i></span>'
                f'<span class="pill">{esc(label)}</span>{extra}')

    def tbl(headers, body):  # headers: list of (text, is_num); body: list of row-html strings
        hh = "".join(f'<th class="num">{esc(t)}</th>' if n else f"<th>{esc(t)}</th>" for t, n in headers)
        return (f'<div class="tw"><table><thead><tr>{hh}</tr></thead><tbody>'
                + "".join(body) + "</tbody></table></div>")

    # ---- headline arm rollup (best = cheapest packed among self-hosted) ----
    sh = [a for a in arms if is_self_hosted(a["model"]) and a["cost_packed_per_task"] != ""]
    best = min(sh, key=lambda a: a["cost_packed_per_task"])["arm"] if sh else None
    arm_body = []
    for a in arms:
        cls = ' class="best"' if a["arm"] == best else ""
        gps = f'{a["gpu_s_per_run"]:.0f}s' if a["gpu_s_per_run"] != "" else "—"
        pk = a.get("peak_concurrency", "")
        passes_label = f"<span class=mut>{a['passes']}/{a['runs']}</span>"
        arm_body.append(
            f"<tr{cls}><td><b>{esc(a['arm'])}</b></td>"
            f"<td>{sbar(a['success_pooled'], rng(a), passes_label)}</td>"
            f"<td class='num'>{a['avg_tokens_out']:,}</td><td class='num'>{gps}</td>"
            f"<td class='num'>{pk if pk != '' else '—'}</td>"
            f"<td class='num'>{usd(a['cost_sole_per_task'])}</td>"
            f"<td class='num'>{usd(a['cost_packed_per_task'])}</td></tr>")
    arm_tbl = tbl([("Arm", 0), ("Success", 0), ("Out Tok", 1), ("GPU-s/run", 1),
                   ("Peak conc", 1), ("$/task sole", 1), ("$/task packed", 1)], arm_body)

    # ---- success by model family (GLM all variants vs API models) ----
    fam = family_rollup(rows)
    def _fam_row(x):
        rate_label = f"{x['success_rate']:.1%}" if x['success_rate'] != '' else '—'
        return (f"<tr><td><b>{esc(x['family'])}</b></td><td class='num'>{x['passes']}</td>"
                f"<td class='num'>{x['runs']}</td>"
                f"<td>{sbar(x['success_rate'], rate_label)}</td></tr>")
    fam_body = [_fam_row(x) for x in fam]
    fam_tbl = tbl([("Model / arm", 0), ("Passed", 1), ("Total", 1), ("Success", 0)], fam_body)

    # ---- cost per completed task (per harness/model/prompt) ----
    def _cost_row(r):
        rate_label = f"{r['success_rate']:.0%}"
        passes_label = f"<span class=mut>{r['passes']}/{r['runs']}</span>"
        return (
            f"<td>{esc(harness_disp(r['harness']))}</td><td>{esc(_mname(r['model']))}</td>"
            f"<td>{esc(r['prompt'])}</td>"
            f"<td>{sbar(r['success_rate'], rate_label, passes_label)}</td>"
            f"<td class='num'>{usd(r['cost_per_successful_task'])}</td>"
            f"<td class='num'>{usd(packed(r))}</td>"
            f"<td class='num'>{(str(round(r['avg_duration_s']))+'s') if r['avg_duration_s']!='' else '—'}</td>")
    cost_body = [_cost_row(r) for r in rows]
    cost_tbl = tbl([("Harness", 0), ("Model", 0), ("Prompt", 0), ("Success", 0),
                    ("$/task sole", 1), ("$/task packed", 1), ("Avg time", 1)],
                   [f"<tr>{c}</tr>" for c in cost_body])

    # ---- concurrency ladder (sole → projected → measured packed) ----
    conc_html = ""
    glm_arms = [a for a in arms if is_self_hosted(a["model"]) and a.get("cost_sole_per_task") != ""]
    if glm_arms:
        conc_body = []
        for a in glm_arms:
            for i, sc in enumerate(concurrency_scenarios(
                    a["cost_sole_per_task"], a.get("avg_concurrency"), a.get("peak_concurrency"),
                    a.get("packing_factor"), a.get("cost_packed_per_task"))):
                arm_cell = (f"<td><b>{esc(a['arm'])}</b></td>" if i == 0
                            else "<td class='mut'>↳</td>")
                tag = ('<span class="tag">measured</span>' if sc["basis"] == "measured"
                       else '<span class="tag">projected</span>')
                cls = ' class="best"' if sc["label"] == "Packed (this run)" else ""
                peak_note = ""
                if sc.get("peak"):
                    peak_note = f' <span class=mut>(peak {sc["peak"]:.0f}×)</span>'
                conc_body.append(
                    f"<tr{cls}>{arm_cell}<td>{esc(sc['label'])}</td>"
                    f"<td class='num'>{sc['conc']:.1f}×{peak_note}</td>"
                    f"<td class='num'>{usd(sc['cost'])}</td><td>{tag}</td></tr>")
        conc_html = (
            "<h2>Concurrency cost ladder</h2>"
            "<p class=note>How <b>$/task</b> changes as you pack concurrent tasks onto the GPU. "
            "<b>Concurrent tasks</b> = generation requests in flight (from opencode step_start→step_finish logs). "
            "<b>Peak instant</b> is the max at any single moment (a spike); <b>avg / packed</b> is the "
            "time-weighted mean over the run (~what the endpoint actually sustained). "
            "<b>Sole</b> and <b>Packed</b> costs are measured; middle rows project ideal linear packing "
            "(<code>sole ÷ N</code>). Measured packed is usually higher than projected at the same avg "
            "because failures, idle gaps between steps, and contention burn GPU time without delivering passes.</p>"
            + tbl([("Arm", 0), ("Scenario", 0), ("Concurrent tasks", 1), ("$/task", 1), ("Basis", 0)],
                  conc_body))

    # ---- efficiency ----
    order = [(r["harness"], r["model"], r["prompt"]) for r in rows]
    glm = next((k for k in order if is_self_hosted(k[1])), None)
    glm_out = eff[glm]["out"] if glm else 0
    def _eff_row(k):
        ratio_label = f"{eff[k]['out']/glm_out:.2f}×" if (glm and glm_out) else '—'
        return (
            f"<tr><td>{esc(harness_disp(k[0]))}</td><td>{esc(_mname(k[1]))}</td><td>{esc(k[2])}</td>"
            f"<td class='num'>{eff[k]['steps']:,}</td><td class='num'>{eff[k]['tools']:,}</td>"
            f"<td class='num'>{eff[k]['out']:,}</td>"
            f"<td class='num'>{ratio_label}</td></tr>")
    eff_body = [_eff_row(k) for k in order]
    eff_tbl = tbl([("Harness", 0), ("Model", 0), ("Prompt", 0), ("Steps", 1), ("Tools", 1),
                   ("Output Tok", 1), ("vs GLM", 1)], eff_body)

    # ---- task difficulty ----
    diff_html = ""
    if crows:
        diff_body = [
            f"<tr><td>{esc(_tshort(r['task']))}</td><td>{esc(r['prompt'])}</td><td>{esc(r['source'])}</td>"
            f"<td class='num'>{esc(r['complexity'])}</td><td class='num'>{r['pass_rate']:.0%}</td>"
            f"<td class='num'>{usd(r.get('avg_api_cost_usd'))}</td>"
            f"<td class='num'>{r['avg_steps']:.0f}</td></tr>" for r in crows]
        diff_html = ("<h2>Task Difficulty</h2><p class=note>Per (task, prompt version). "
                     "Source: swe-bench = real dataset issue embedded verbatim; invented = task + prompt we wrote.</p>"
                     + tbl([("Task", 0), ("Prompt", 0), ("Source", 0), ("Diff", 1), ("Pass", 1),
                            ("API $", 1), ("Avg steps", 1)], diff_body))

    # ---- #1 cost spread + #2 throughput (per arm, from per-run `detailed`) ----
    perf_html = ""
    if detailed:
        costs, gen_s, out_tok = {}, {}, {}
        for d in detailed:
            k = (d["harness"], d["model"])
            c, cs, to = _f(d.get("cost_usd")), _f(d.get("call_s")), _f(d.get("tokens_out"))
            if c is not None:
                costs.setdefault(k, []).append(c)
            gen_s[k] = gen_s.get(k, 0.0) + (cs or 0)
            out_tok[k] = out_tok.get(k, 0.0) + (to or 0)
        eff_arm = {}
        for (h, m, pv), v in eff.items():
            e = eff_arm.setdefault((h, m), {"steps": 0, "tools": 0})
            e["steps"] += v["steps"]; e["tools"] += v["tools"]

        def q(vals, p):
            if not vals:
                return None
            s = sorted(vals)
            return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]
        perf_body = []
        for a in arms:
            k = (a["harness"], a["model"])
            cv = costs.get(k, [])
            med, p90 = q(cv, 0.5), q(cv, 0.9)
            sd = statistics.pstdev(cv) if len(cv) > 1 else 0.0
            tps = (out_tok.get(k, 0) / gen_s[k]) if gen_s.get(k) else None
            e = eff_arm.get(k, {"steps": 0, "tools": 0})
            rn = a["runs"] or 1
            perf_body.append(
                f"<tr><td><b>{esc(a['arm'])}</b></td>"
                f"<td class='num'>{usd(med) if med is not None else '—'}</td>"
                f"<td class='num'>{usd(p90) if p90 is not None else '—'}</td>"
                f"<td class='num'>{('$%.3f' % sd)}</td>"
                f"<td class='num'>{('%.0f' % tps) if tps else '—'}</td>"
                f"<td class='num'>{e['steps']/rn:.1f}</td>"
                f"<td class='num'>{e['tools']/rn:.1f}</td></tr>")
        perf_html = ("<h2>Cost spread &amp; throughput</h2>"
                     "<p class=note>Per-run cost distribution (median / p90 / std-dev) shows how <i>reliable</i> "
                     "each arm's cost is, not just the mean. Tok/s = output tokens ÷ generation seconds; "
                     "steps &amp; tools per run show <i>why</i> — fewer turns = cheaper. (Reasoning-vs-answer token "
                     "split isn't in the logs; total output in the rollup above is the proxy.)</p>"
                     + tbl([("Arm", 0), ("Median $", 1), ("p90 $", 1), ("Std-dev $", 1),
                            ("Tok/s", 1), ("Steps/run", 1), ("Tools/run", 1)], perf_body))

    # ---- #3 per-task × arm pass-rate matrix (from `detailed`) ----
    matrix_html = ""
    if detailed:
        arm_seq = [a["arm"] for a in arms]
        cell = {}
        tasks_seen = []
        for d in detailed:
            t = _tshort(d["task"])
            if t not in tasks_seen:
                tasks_seen.append(t)
            al = _arm_label(d["harness"], d["model"])
            pt = cell.setdefault((t, al), [0, 0])
            pt[1] += 1
            pt[0] += 1 if d["status"] == "pass" else 0
        head = [("Task", 0)] + [(a, 1) for a in arm_seq]
        mbody = []
        for t in tasks_seen:
            tds = [f"<td>{esc(t)}</td>"]
            for al in arm_seq:
                p, tot = cell.get((t, al), [0, 0])
                if tot == 0:
                    tds.append("<td class='num mut'>·</td>")
                    continue
                r = p / tot
                bg = "var(--best)" if r == 1 else ("rgba(220,38,38,.16)" if r == 0 else "rgba(217,119,6,.16)")
                tds.append(f"<td class='num' style='background:{bg}'>{p}/{tot}</td>")
            mbody.append("<tr>" + "".join(tds) + "</tr>")
        matrix_html = ("<h2>Per-task × arm pass matrix</h2>"
                       "<p class=note>Where each arm actually passes or fails (pooled across prompt versions + runs). "
                       "Green = all pass, red = all fail, amber = partial. This is where GLM's capability gap vs Opus shows.</p>"
                       + tbl(head, mbody))

    # ---- #4 break-even concurrency vs Opus (derived from measured sole cost) ----
    be_html = ""
    api_sole = [a["cost_sole_per_task"] for a in arms
                if not is_self_hosted(a["model"]) and a["cost_sole_per_task"] != ""]
    opus_flat = min(api_sole) if api_sole else None
    if opus_flat:
        be_body = []
        for a in arms:
            if not is_self_hosted(a["model"]) or a["cost_sole_per_task"] == "":
                continue
            nstar = a["cost_sole_per_task"] / opus_flat
            pk = a.get("peak_concurrency", "")
            verdict = ("✓ reached" if (pk != "" and pk >= nstar) else "not yet")
            be_body.append(
                f"<tr><td><b>{esc(a['arm'])}</b></td>"
                f"<td class='num'>{usd(a['cost_sole_per_task'])}</td>"
                f"<td class='num'>{usd(opus_flat)}</td>"
                f"<td class='num'>{nstar:.1f}×</td>"
                f"<td class='num'>{pk if pk != '' else '—'}</td>"
                f"<td>{verdict}</td></tr>")
        be_html = ("<h2>Break-even concurrency vs Opus</h2>"
                   f"<p class=note>Opus is per-token (flat ${opus_flat:.3f}/task). A GLM arm matches it once it runs "
                   "≈ (GLM sole ÷ Opus) tasks in parallel. ⚠️ Assumes <i>ideal</i> linear packing — real "
                   "break-even is higher once you account for idle time and contention.</p>"
                   + tbl([("Arm", 0), ("GLM $/task (alone)", 1), ("Opus flat $", 1),
                          ("Break-even conc.", 1), ("Peak this run", 1), ("Status", 0)], be_body))

    ntask = len({r["task"] for r in crows}) if crows else 0
    best_packed = min((a["cost_packed_per_task"] for a in sh), default="")
    chips = [("GPU rate", f"${GPU_HOURLY_USD:.2f}/hr"), ("Arms", str(len(arms))),
             ("Tasks", str(ntask)), ("Prompt versions", str(len({r["prompt"] for r in rows})))]
    if best_packed != "":
        chips.append(("Best $/task packed", usd(best_packed)))
    if overall_peak:
        chips.append(("Peak concurrency", str(overall_peak)))
    avg_overall = ""
    if glm_arms:
        avgs = [_f(a.get("avg_concurrency")) for a in glm_arms if _f(a.get("avg_concurrency"))]
        if avgs:
            avg_overall = round(sum(avgs) / len(avgs), 1)
            chips.append(("Avg concurrency", f"{avg_overall}×"))
    chips_html = "".join(f'<div class="chip"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for k, v in chips)
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")

    # --- "How this benchmark works": plain-English method + an SVG of the generate→grade pipeline ---
    swe_tasks = {r["task"] for r in crows if "swebench" in r["task"].lower()} if crows else set()
    nswe = len(swe_tasks) or ntask
    nrepo = len({t.split("demo-swebench-", 1)[-1].rsplit("-", 1)[0] for t in swe_tasks})
    _proj = f", across {nrepo} projects" if nrepo else ""
    _box = "fill='var(--card)' stroke='var(--border)' rx='12'"
    method_html = f"""
<h2>How this benchmark works</h2>
<p class="note">We measure one thing: <b>what it costs to actually finish a coding task</b> — not tokens,
not leaderboard scores. A task counts only when the project's own tests flip from failing to passing.
Every model runs the same {nswe} real GitHub issues (SWE-bench Verified{_proj}, from 15-minute fixes to
multi-hour ones) through the same harness; the only things we change are the model and its settings.</p>
<div class="tw" style="padding:16px 10px">
<svg viewBox="0 0 960 200" role="img" aria-label="Pipeline: generate locally, then grade in the cloud"
     style="width:100%;height:auto;max-width:960px;display:block;margin:auto">
  <defs><marker id="arw" markerWidth="10" markerHeight="10" refX="7.5" refY="3" orient="auto">
    <path d="M0,0 L7.5,3 L0,6 Z" fill="var(--muted)"/></marker></defs>
  <text x="6" y="26" font-size="12" fill="var(--muted)">Two phases: <tspan fill="var(--fg)" font-weight="600">generate locally</tspan>, then <tspan fill="var(--fg)" font-weight="600">grade in the cloud</tspan> — the same split the official SWE-bench uses.</text>
  <rect x="6" y="54" width="150" height="92" {_box}/>
  <text x="81" y="84" text-anchor="middle" font-size="14" font-weight="700" fill="var(--fg)">Tasks</text>
  <text x="81" y="105" text-anchor="middle" font-size="11" fill="var(--muted)">{nswe} real issues</text>
  <text x="81" y="121" text-anchor="middle" font-size="11" fill="var(--muted)">SWE-bench Verified</text>
  <rect x="176" y="54" width="170" height="92" {_box}/>
  <text x="190" y="84" font-size="13.5" font-weight="700" fill="var(--fg)"><tspan fill="var(--accent)">1 · </tspan>Generate</text>
  <text x="190" y="105" font-size="11" fill="var(--muted)">bench.sh · local</text>
  <text x="190" y="121" font-size="11" fill="var(--muted)">agent writes a fix</text>
  <text x="261" y="170" text-anchor="middle" font-size="10.5" fill="var(--good)">records GPU-seconds → cost</text>
  <rect x="366" y="54" width="170" height="92" {_box}/>
  <text x="380" y="84" font-size="13.5" font-weight="700" fill="var(--fg)"><tspan fill="var(--accent)">2 · </tspan>Harvest</text>
  <text x="380" y="105" font-size="11" fill="var(--muted)">make_predictions</text>
  <text x="380" y="121" font-size="11" fill="var(--muted)">collect the diffs</text>
  <rect x="556" y="54" width="170" height="92" {_box}/>
  <text x="570" y="84" font-size="13.5" font-weight="700" fill="var(--fg)"><tspan fill="var(--accent)">3 · </tspan>Grade</text>
  <text x="570" y="105" font-size="11" fill="var(--muted)">Modal · x86 Docker</text>
  <text x="570" y="121" font-size="11" fill="var(--muted)">run project tests</text>
  <text x="641" y="170" text-anchor="middle" font-size="10.5" fill="var(--good)">official pass / fail</text>
  <rect x="746" y="54" width="170" height="92" {_box}/>
  <text x="760" y="84" font-size="13.5" font-weight="700" fill="var(--fg)"><tspan fill="var(--accent)">4 · </tspan>Report</text>
  <text x="760" y="105" font-size="11" fill="var(--muted)">aggregate</text>
  <text x="760" y="121" font-size="11" fill="var(--muted)">cost ÷ passes</text>
  <line x1="158" y1="100" x2="174" y2="100" stroke="var(--muted)" stroke-width="1.6" marker-end="url(#arw)"/>
  <line x1="348" y1="100" x2="364" y2="100" stroke="var(--muted)" stroke-width="1.6" marker-end="url(#arw)"/>
  <line x1="538" y1="100" x2="554" y2="100" stroke="var(--muted)" stroke-width="1.6" marker-end="url(#arw)"/>
  <line x1="728" y1="100" x2="744" y2="100" stroke="var(--muted)" stroke-width="1.6" marker-end="url(#arw)"/>
</svg>
</div>
<p class="note"><b>1 · Generate</b>: <code>bench.sh</code> hands each agent the bug report and
the code; it writes a fix. We record the seconds the model spent generating — that time × the GPU's
hourly rate is the cost. <b>2 · Harvest</b>: <code>make_predictions.py</code> pulls each attempt's change
(a git diff). <b>3 · Grade</b> (cloud): <code>swe_eval_modal.py</code> runs each fix inside that project's
official SWE-bench Docker image — the exact old Python and dependencies it needs — and runs the project's
real test suite. A fix passes only if every target test <i>and</i> every already-passing test still
passes. <b>4 · Report</b>: <code>aggregate.py</code> divides cost by the fixes that actually worked.</p>
<p class="note"><b>Why two machines?</b> Writing the fix needs the model (fast, local); grading a
years-old project needs its exact environment, which only lives in that project's container — so grading
runs in the cloud. It is the same method the official SWE-bench uses, so our pass/fail matches the
published standard. Each task also ships a known-good <i>gold</i> fix, and we confirm the grader marks it
passing before trusting the task.</p>
<p class="note"><b>What we vary:</b> reasoning effort (default / high / off) and prompt wording
(v1 baseline · v2 shaped · v3 control) — nothing else is changed between models. <b>Cost basis:</b>
self-hosted GLM = GPU-seconds × hourly rate; Claude = tokens × list price. <b>&ldquo;sole&rdquo;</b> = one
task at a time on the endpoint; <b>&ldquo;packed&rdquo;</b> = the endpoint shared by concurrent tasks,
where self-hosting gets cheap.</p>"""

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coding Agent Cost Bench — {esc(os.path.basename(os.path.normpath(results_dir)))}</title>
<style>{_HTML_CSS}</style></head><body><div class="wrap">
<h1>Coding Agent Cost Bench</h1>
<p class="sub">{esc(results_dir)} · generated {gen}</p>
<div class="chips">{chips_html}</div>
{method_html}
<h2>Reasoning / Arm Rollup</h2>
<p class="note">One row per arm, pooled across prompt versions (success = range over v1–v3). Costs are
passes-weighted. Sweet spot (cheapest packed → completed task) is highlighted; usually
<b>high reasoning</b> &mdash; decisive, fewest turns &mdash; while <b>max reasoning</b> over-thinks and
<b>no thinking</b> thrashes. <b>Peak conc</b> = max simultaneous generation requests at one instant;
<b>avg conc</b> = sustained mean over the run{f' (overall peak this run: {overall_peak})' if overall_peak else ''}.</p>
{arm_tbl}

<h2>Success by Model</h2>
<p class="note">GLM-5.2 broken out by reasoning tier (max / high / no thinking), with each API model separate.
The headline per-variant success comparison.</p>
{fam_tbl}

<h2>Cost per Completed Task</h2>
<p class="note">$/task <b>sole</b> = one task alone on the GPU (its own generation time × rate).
$/task <b>packed</b> = endpoint filled with concurrent tasks (union of generation ÷ tasks). The gap is
the concurrency lever; API models are per-token, so the columns match for them.</p>
{cost_tbl}

{conc_html}

<h2>Efficiency</h2>
<p class="note">Steps, tool calls, and output tokens per arm; “vs GLM” compares output volume to the GLM baseline.</p>
{eff_tbl}

{perf_html}
{be_html}
{matrix_html}
{diff_html}
<footer>Self-contained report written by aggregate.py. Numbers mirror summary.csv / arms.csv / complexity.csv.</footer>
</div></body></html>"""
    path = os.path.join(results_dir, "report.html")
    with open(path, "w") as f:
        f.write(doc)
    return path


if __name__ == "__main__":
    main()
