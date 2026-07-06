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
import sys
from collections import defaultdict
from datetime import datetime

RESULTS_DIR = os.environ.get("RESULTS_DIR", "./results")

# Hourly run-rate of the 8xB200 auto-endpoint while it's actively serving. Measured from this
# workspace's own Modal billing ($32.08 over the 38 active minutes of the first run = $50.7/hr,
# i.e. ~$6.34 per B200-hour x 8). Override with GLM_GPU_HOURLY_USD if the GPU/count differs.
GPU_HOURLY_USD = float(os.environ.get("GLM_GPU_HOURLY_USD", "50.7"))


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


def main():
    manifest = os.path.join(RESULTS_DIR, "manifest.csv")
    if not os.path.exists(manifest):
        sys.exit(f"no manifest at {manifest} — run run_bench.sh first")

    detailed = []
    # arm = (harness, model, prompt-version): the prompt version is a first-class sweep dimension,
    # so v1 vs v2 of the SAME (harness, model) are separate rows and never pooled together.
    intervals = defaultdict(list)   # arm -> [(start, end), ...] run intervals (uptime)
    genivs = defaultdict(list)      # arm -> [(start, end), ...] generation windows (concurrency-safe)
    eff = defaultdict(lambda: {"steps": 0, "tools": 0, "prose": 0, "reason": 0, "out": 0})
    task_stat = defaultdict(lambda: {"runs": 0, "steps": 0, "tools": 0, "out": 0, "dur": 0.0, "pass": 0, "api_cost": 0.0})
    with open(manifest) as f:
        for row in csv.DictReader(f):
            m = row["model"]
            h = row.get("harness") or harness_of(m)   # harness recorded by run_bench (fallback for old data)
            pv = row.get("prompt") or "v1"             # prompt version (fallback for pre-sweep manifests)
            key = (h, m, pv)
            s, e = _f(row.get("start")), _f(row.get("end"))
            if s is not None and e is not None:
                intervals[key].append((s, e))
            if h == "claude":                          # Claude Code reports its own cost/usage/turns
                pm = claude_stats(row.get("outdir", ""))
                tin, tout, ccost, cs, basis = pm["tin"], pm["tout"], pm["cost"], pm["call_s"], "claude_code"
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
    _print_report(RESULTS_DIR, rows, eff, crows, intervals)


def _mname(model):
    """Friendly model name (no harness), e.g. GLM-5.2 FP8 (no thinking) / Claude Opus 4.8.
    Harness is shown in its own column, so it's not folded in here."""
    prov, last = model.split("/")[0], model.split("/")[-1]
    name = {"claude-opus-4-8": "Claude Opus 4.8"}.get(last, last.replace("-FP8", " FP8"))
    tag = {"modal-nothink": " (no thinking)", "modal-high": " (high reasoning)"}.get(prov, "")
    return name + tag


def _tshort(t):
    t = t.replace("demo-swebench-", "").replace("demo-", "")
    return t.split("__")[-1] if "__" in t else t


def _print_report(results_dir, rows, eff, crows, intervals):
    W = 79
    def rule(ch):
        print(ch * W)
    def section(title):
        print("\n" + "═" * W + f"\n {title}\n" + "═" * W)
    def table(headers, body):
        cols = list(zip(*([headers] + body)))
        w = [max(len(str(c)) for c in col) for col in cols]
        pad = lambda vals: "  " + "  ".join(str(v).ljust(w[j]) for j, v in enumerate(vals))
        print(pad(headers))
        print("  " + "  ".join("─" * w[j] for j in range(len(headers))))
        for b in body:
            print(pad(b))

    rule("─"); print(" Coding Agent Cost Bench"); rule("─")
    print("\n✓ Results written:")
    print(f"    {results_dir}/results_detailed.csv")
    print(f"    {results_dir}/summary.csv")
    print("\nGPU pricing")
    print(f"  GLM endpoint: ${GPU_HOURLY_USD:.2f}/hr (charged only while generating)")

    def usd(v):
        return f"${v:.3f}" if v not in ("", None) else "—"

    def packed_per_task(r):
        # GLM: shared-endpoint wall-clock (gen union) ÷ passes. API/Claude: per-token = same as sole.
        if is_self_hosted(r["model"]):
            w, p = _f(r.get("gpu_wall_cost_usd")), r.get("passes")
            return w / p if (w and p) else ""
        return r.get("cost_per_successful_task")

    section("Cost per Completed Task")
    print()
    table(["Harness", "Model", "Prompt", "Success", "$/task sole", "$/task packed", "Avg Time"],
          [[harness_disp(r["harness"]), _mname(r["model"]), r["prompt"],
            f"{r['passes']}/{r['runs']}  {r['success_rate']:.0%}",
            usd(r["cost_per_successful_task"]), usd(packed_per_task(r)),
            f"{r['avg_duration_s']:.0f}s" if r["avg_duration_s"] != "" else "—"] for r in rows])
    print("\n  $/task sole   = one task alone on the 8×B200 — its own generation time × rate (no packing).")
    print("  $/task packed = endpoint filled with concurrent tasks — union of generation ÷ tasks.")
    print(f"  Both count generation only (step_start→step_finish × ${GPU_HOURLY_USD:.2f}/hr, no local scripts).")
    print("  The gap between them is the concurrency lever. API models are per-token (concurrency-")
    print("  invariant), so both columns match for Claude/GPT/Gemini.")
    print("  Prompt = task prompt version (v1 baseline / v2 shaped / v3 control; see PROMPTS.md).")

    st = [r for r in rows if is_self_hosted(r["model"]) and r["cost_per_successful_task"] != "" and r["passes"]]
    if st:
        section("If you own the GPU (single tenant)")
        for r in st:
            gen = r["cost_per_successful_task"]
            idle_c = (_f(r["idle_s"]) or 0) / 3600 * GPU_HOURLY_USD / r["passes"]
            print(f"\n {_mname(r['model'])}  [{harness_disp(r['harness'])} · prompt {r['prompt']}]")
            print(f"   generation   ${gen:.3f}")
            print(f" + idle tax     ${idle_c:.3f}")
            print(" " + "─" * 22)
            print(f" = total        ${gen + idle_c:.3f} / task")
        print("\n  Idle tax = endpoint up but not generating (local pip/pytest/git) while this arm runs alone.")
        print("  Recoverable by packing the endpoint with concurrent work — other users/tasks filling the gaps.")

    if st:
        section("GPU Utilization")
        print()
        table(["Harness", "Model", "Prompt", "Uptime", "Generating", "Idle"],
              [[harness_disp(r["harness"]), _mname(r["model"]), r["prompt"],
                f"{_f(r['active_s']) or 0:.0f} s", f"{_f(r['gen_s']) or 0:.0f} s",
                f"{_f(r['idle_s']) or 0:.0f} s"] for r in st])

    order = [(r["harness"], r["model"], r["prompt"]) for r in rows]
    glm = next((k for k in order if is_self_hosted(k[1])), None)
    glm_out = eff[glm]["out"] if glm else 0
    section("Efficiency")
    print()
    table(["Harness", "Model", "Prompt", "Steps", "Tools", "Output Tok", "vs GLM"],
          [[harness_disp(k[0]), _mname(k[1]), k[2], f"{eff[k]['steps']:,}", f"{eff[k]['tools']:,}", f"{eff[k]['out']:,}",
            f"{eff[k]['out'] / glm_out:.2f}×" if glm and glm_out else "—"] for k in order])

    if crows:
        section("Task Difficulty")
        print()
        table(["Task", "Prompt", "Source", "Diff", "Pass", "API $", "Avg Steps"],
              [[_tshort(r["task"]), r["prompt"], r["source"], f"{r['complexity']}", f"{r['pass_rate']:.0%}",
                usd(r.get("avg_api_cost_usd")),
                f"{r['avg_steps']:.0f}"] for r in crows])
        print("\n  Diff is per (task, prompt version): v1 (terse/raw) usually demands more than v2 (shaped).")
        print("  Source: swe-bench = real dataset issue (embedded verbatim) in our template; "
              "invented = task + prompt we wrote.")
    print()


if __name__ == "__main__":
    main()
