#!/usr/bin/env python3
"""
Aggregate benchmark results -> results/{results_detailed,summary}.csv.

ONE cost per row, always labeled with its basis. Two currencies:
  * API models (Claude/GPT/Gemini) -> cost = tokens x per-token price (how the provider
    bills). From ccusage. basis "api_ccusage".
  * GLM on Modal (self-host)        -> charged on ENDPOINT CALL TIME only: sum of the seconds
    the agent actually spent generating on the endpoint (parsed from output.log), x the hourly
    rate. basis "gpu_calls". This deliberately EXCLUDES local script time (pip/pytest/git/tool
    exec) where the GPU is idle. Attribution caveat: Modal bills container UPTIME, not compute-
    seconds, so a sole tenant really pays for wall-clock (see active_s) incl. that idle — the
    call-only number is the fair shared-endpoint / packed floor. The report spells this out.

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
      call_s  - endpoint generation time: sum over steps of (first response part - step_start),
                i.e. only the model calls, excluding local tool/script gaps (pip/pytest/git).
      steps   - assistant turns; tools - tool calls; prose - chars of natural-language text;
      reason  - reasoning tokens; out - output tokens (tool args + prose the model emitted)."""
    log = os.path.join(outdir, "output.log")
    m = {"call_s": 0.0, "steps": 0, "tools": 0, "prose": 0, "reason": 0, "out": 0}
    if not os.path.exists(log):
        return m
    call_ms = 0
    start = None
    counted = False
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
            start, counted, m["steps"] = ts, False, m["steps"] + 1
        elif t == "tool_use":
            m["tools"] += 1
            if start is not None and not counted and ts is not None:
                call_ms += ts - start
                counted = True
        elif t == "text":
            m["prose"] += len(p.get("text", "") or "")
            if start is not None and not counted and ts is not None:
                call_ms += ts - start
                counted = True
        elif t == "step_finish":
            tk = p.get("tokens", {}) or {}
            m["out"] += tk.get("output", 0) or 0
            m["reason"] += tk.get("reasoning", 0) or 0
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
    first tool_use/text), from an opencode output.log. Unioning these across concurrent runs gives
    concurrency-correct generation wall-clock — so idle = uptime_union - gen_union is always >= 0
    (unlike active_s - sum(call_s), which breaks when parallel runs generate at the same time)."""
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
        elif t in ("tool_use", "text") and start is not None and ts is not None:
            out.append((start / 1000.0, ts / 1000.0))     # ms epoch -> s
            start = None
        elif t == "step_finish":
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
    task_stat = defaultdict(lambda: {"runs": 0, "steps": 0, "tools": 0, "out": 0, "dur": 0.0, "pass": 0})
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
            detailed.append({
                "task": row["task"], "harness": h, "model": m, "prompt": pv, "run": row["run"],
                "status": row["status"],
                "start": _iso(s), "end": _iso(e), "duration_s": row.get("duration_s", ""),
                "call_s": round(cs, 1),
                "tokens_in": tin, "tokens_out": tout,
                "cost_usd": "" if is_self_hosted(m) else round(ccost, 6),
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
        call_total = sum(v for r in runs if (v := _f(r.get("call_s"))) is not None)   # generation WORK (sum)
        gen_wall = union_seconds(genivs[key])                         # generation WALL-CLOCK (union; concurrency-safe)
        idle = max(0.0, active - gen_wall)                            # up but not generating anything (>= 0)
        row = {
            "harness": harness, "model": model, "prompt": prompt, "runs": n, "passes": passes,
            "success_rate": round(passes / n, 3),
            "avg_tokens_in": round(statistics.mean(r["tokens_in"] for r in runs)),
            "avg_tokens_out": round(statistics.mean(r["tokens_out"] for r in runs)),
            "avg_duration_s": round(statistics.mean(durs), 1) if durs else "",
            "call_s": round(call_total, 1) if call_total else "",     # total generation work (info)
            "gen_s": round(gen_wall, 1) if gen_wall else "",          # generation wall-clock (billed basis)
            "active_s": round(active, 1) if active else "",
            "idle_s": round(idle, 1) if (intervals[key] and gen_wall) else "",
            "overlap_s": round(overlap, 1) if intervals[key] else "",
            "total_cost_usd": "", "cost_per_successful_task": "", "cost_basis": "",
        }
        if is_self_hosted(model):
            # cost = generation WALL-CLOCK × rate (union of gen windows, so concurrent tasks aren't
            # double-counted). idle = uptime − gen wall-clock >= 0. sole-tenant = uptime × rate.
            if gen_wall:
                cost = gen_wall / 3600 * GPU_HOURLY_USD
                row["total_cost_usd"] = round(cost, 4)
                row["cost_per_successful_task"] = round(cost / passes, 4) if passes else ""
                row["cost_basis"] = "gpu_gen"
            else:
                row["cost_basis"] = "gpu_gen (no log timing)"
        else:   # token-billed (opencode APIs) or Claude Code (reports its own $)
            total = sum(r["cost_usd"] for r in runs if r["cost_usd"] != "")
            row["total_cost_usd"] = round(total, 4)
            row["cost_per_successful_task"] = round(total / passes, 4) if passes else ""
            row["cost_basis"] = runs[0]["cost_basis"]
        rows.append(row)

    with open(os.path.join(RESULTS_DIR, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # write complexity.csv (data), then render the console report
    comp = task_complexity(task_stat)   # keyed by (task, prompt version)
    crows = [{"task": t[0], "prompt": t[1], "source": task_source(t[0]), "complexity": a["complexity"],
              "pass_rate": a["pass_rate"], "runs": a["runs"],
              "avg_steps": round(a["avg_steps"], 1), "avg_tools": round(a["avg_tools"], 1),
              "avg_out_tok": round(a["avg_out"]), "avg_dur_s": round(a["avg_dur"], 1)}
             for t, a in sorted(comp.items(), key=lambda kv: kv[1]["complexity"], reverse=True)]
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

    section("Cost per Completed Task")
    print()
    table(["Harness", "Model", "Prompt", "Success", "Cost/task", "Avg Time"],
          [[harness_disp(r["harness"]), _mname(r["model"]), r["prompt"],
            f"{r['passes']}/{r['runs']}  {r['success_rate']:.0%}",
            usd(r["cost_per_successful_task"]),
            f"{r['avg_duration_s']:.0f}s" if r["avg_duration_s"] != "" else "—"] for r in rows])
    print("\n  Cost/task = generation only (what the model actually spent on the GPU).")
    print("  Prompt = which task prompt version ran (v1 = baseline/default; v2 = shaped template; see PROMPTS.md).")

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
        table(["Task", "Prompt", "Source", "Diff", "Pass", "Avg Steps"],
              [[_tshort(r["task"]), r["prompt"], r["source"], f"{r['complexity']}", f"{r['pass_rate']:.0%}",
                f"{r['avg_steps']:.0f}"] for r in crows])
        print("\n  Diff is per (task, prompt version): v1 (terse/raw) usually demands more than v2 (shaped).")
        print("  Source: swe-bench = real dataset issue (embedded verbatim) in our template; "
              "invented = task + prompt we wrote.")
    print()


if __name__ == "__main__":
    main()
