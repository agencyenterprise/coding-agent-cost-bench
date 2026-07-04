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
    """Self-hosted (GPU-billed) = served via the modal/ provider."""
    return model.lower().startswith("modal/")


def harness_of(ref):
    """Which agent harness ran this model ref: Claude Code's CLI vs opencode."""
    return "claude-code" if ref.startswith("claude-code/") else "opencode"


def model_id(ref):
    """The model itself, without the harness/provider prefix (e.g. claude-opus-4-8)."""
    return ref.split("/", 1)[1] if "/" in ref else ref


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
    """Empirical, RELATIVE task complexity from observed effort pooled across ALL runs/models:
    mean steps, tool calls, output tokens, and duration — each min-max normalized across the task
    set, averaged, scaled to 0-10. Higher = the task demanded more work. It's relative to this set,
    not absolute. pass_rate is reported alongside (outcome, kept separate from effort)."""
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
    intervals = defaultdict(list)   # (harness,model) -> [(start, end), ...] for GPU costing
    eff = defaultdict(lambda: {"steps": 0, "tools": 0, "prose": 0, "reason": 0, "out": 0})
    task_stat = defaultdict(lambda: {"runs": 0, "steps": 0, "tools": 0, "out": 0, "dur": 0.0, "pass": 0})
    with open(manifest) as f:
        for row in csv.DictReader(f):
            m = row["model"]
            h = row.get("harness") or harness_of(m)   # harness recorded by run_bench (fallback for old data)
            key = (h, m)
            s, e = _f(row.get("start")), _f(row.get("end"))
            if s is not None and e is not None:
                intervals[key].append((s, e))
            if h == "claude":                          # Claude Code reports its own cost/usage/turns
                pm = claude_stats(row.get("outdir", ""))
                tin, tout, ccost, cs, basis = pm["tin"], pm["tout"], pm["cost"], pm["call_s"], "claude_code"
            else:
                tin, tout, ccost = load_usage(row.get("outdir", ""))
                pm = log_stats(row.get("outdir", ""))  # work/efficiency metrics from opencode log
                cs = pm["call_s"]
                basis = "gpu_calls" if is_self_hosted(m) else "api_ccusage"
            for k in eff[key]:
                eff[key][k] += pm[k]
            ts = task_stat[row["task"]]                 # pool all runs for empirical task complexity
            ts["runs"] += 1
            ts["steps"] += pm["steps"]; ts["tools"] += pm["tools"]; ts["out"] += pm["out"]
            ts["dur"] += _f(row.get("duration_s")) or 0.0
            ts["pass"] += 1 if row["status"] == "pass" else 0
            detailed.append({
                "task": row["task"], "harness": h, "model": m, "run": row["run"],
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

    by_model = defaultdict(list)
    for d in detailed:
        by_model[(d["harness"], d["model"])].append(d)

    rows = []
    for (harness, model), runs in by_model.items():
        key = (harness, model)
        n = len(runs)
        passes = sum(1 for r in runs if r["status"] == "pass")
        durs = [_f(r["duration_s"]) for r in runs if _f(r["duration_s"]) is not None]
        active = union_seconds(intervals[key])                        # wall-clock union = sole-tenant uptime
        overlap = sum(e - s for s, e in intervals[key]) - active      # time compressed by parallelism
        call_total = sum(v for r in runs if (v := _f(r.get("call_s"))) is not None)
        idle = max(0.0, active - call_total)                          # up but NOT generating
        row = {
            "harness": harness, "model": model, "runs": n, "passes": passes,
            "success_rate": round(passes / n, 3),
            "avg_tokens_in": round(statistics.mean(r["tokens_in"] for r in runs)),
            "avg_tokens_out": round(statistics.mean(r["tokens_out"] for r in runs)),
            "avg_duration_s": round(statistics.mean(durs), 1) if durs else "",
            "call_s": round(call_total, 1) if call_total else "",
            "active_s": round(active, 1) if active else "",
            "idle_s": round(idle, 1) if (intervals[key] and call_total) else "",
            "overlap_s": round(overlap, 1) if intervals[key] else "",
            "gen_usd_task": "", "idle_usd_task": "", "sole_usd_task": "",
            "total_cost_usd": "", "cost_per_successful_task": "", "cost_basis": "",
        }
        if is_self_hosted(model):
            if call_total:
                cost = call_total / 3600 * GPU_HOURLY_USD   # billed on generation time only
                row["total_cost_usd"] = round(cost, 4)
                if passes:
                    # sole-tenant $/task = generation (floor) + idle tax
                    row["cost_per_successful_task"] = round(cost / passes, 4)
                    row["gen_usd_task"] = round(cost / passes, 4)
                    row["idle_usd_task"] = round(idle / 3600 * GPU_HOURLY_USD / passes, 4)
                    row["sole_usd_task"] = round(active / 3600 * GPU_HOURLY_USD / passes, 4)
                row["cost_basis"] = "gpu_calls"
            else:
                row["cost_basis"] = "gpu_calls (no log timing)"
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

    def _show(v):
        return "—" if v in ("", None) else str(v)

    print(f"wrote {RESULTS_DIR}/results_detailed.csv + {RESULTS_DIR}/summary.csv")
    print(f"(GLM GPU rate: ${GPU_HOURLY_USD:.2f}/hr, charged on endpoint call time only)\n")
    headers = ["harness", "model", "pass", "succ", "avg_s", "call_s", "uptime_s", "idle_s",
               "overlap_s", "tok_in", "tok_out", "$/task", "basis"]
    table = [headers]
    for r in rows:
        table.append([
            r["harness"],
            model_id(r["model"]),
            f"{r['passes']}/{r['runs']}",
            f"{r['success_rate']:.0%}",
            _show(r["avg_duration_s"]),
            _show(r["call_s"]),
            _show(r["active_s"]),
            _show(r["idle_s"]),
            _show(r["overlap_s"]),
            _show(r["avg_tokens_in"]),
            _show(r["avg_tokens_out"]),
            _show(r["cost_per_successful_task"]),
            _show(r["cost_basis"]),
        ])
    w = [max(len(str(row[i])) for row in table) for i in range(len(headers))]
    for i, row in enumerate(table):
        print("  " + "  ".join(str(c).ljust(w[j]) for j, c in enumerate(row)))
        if i == 0:
            print("  " + "  ".join("-" * w[j] for j in range(len(headers))))

    # sole-tenant $/task decomposition for the GPU-billed model(s)
    for r in rows:
        if r["sole_usd_task"] != "":
            print(f"\n  {r['model']} — sole-tenant ${r['sole_usd_task']}/task  =  "
                  f"generation ${r['gen_usd_task']} (floor)  +  idle tax ${r['idle_usd_task']}")

    # efficiency comparison (transposed: metric rows x (harness,model) columns) + ratio vs GLM
    order = [(r["harness"], r["model"]) for r in rows]
    glm = next((k for k in order if is_self_hosted(k[1])), None)
    abbr = {"opencode": "oc", "claude": "cc"}
    lab = lambda k: abbr.get(k[0], k[0]) + ":" + model_id(k[1])
    others = [k for k in order if k != glm]
    metrics = [("steps (turns)", "steps"), ("tool calls", "tools"), ("output tokens", "out"),
               ("prose chars", "prose"), ("reasoning tokens", "reason")]
    et = [["metric"] + [lab(k) for k in order] + ([f"GLM÷{lab(k)}" for k in others] if glm else [])]
    for name, mk in metrics:
        line = [name] + [f"{eff[k][mk]:,}" for k in order]
        if glm:
            line += [f"{eff[glm][mk] / eff[k][mk]:.1f}×" if eff[k][mk] else "—" for k in others]
        et.append(line)
    ew = [max(len(str(row[i])) for row in et) for i in range(len(et[0]))]
    print("\n  efficiency (totals; same harness & prompts):")
    for i, row in enumerate(et):
        print("  " + "  ".join(str(c).ljust(ew[j]) for j, c in enumerate(row)))
        if i == 0:
            print("  " + "  ".join("-" * ew[j] for j in range(len(et[0]))))

    # empirical task complexity (relative 0-10 from observed effort pooled across all models)
    comp = task_complexity(task_stat)
    ccols = ["task", "complexity", "pass_rate", "runs", "avg_steps", "avg_tools", "avg_out_tok", "avg_dur_s"]
    crows = []
    for t, a in sorted(comp.items(), key=lambda kv: kv[1]["complexity"], reverse=True):
        crows.append({"task": t, "complexity": a["complexity"], "pass_rate": a["pass_rate"],
                      "runs": a["runs"], "avg_steps": round(a["avg_steps"], 1),
                      "avg_tools": round(a["avg_tools"], 1), "avg_out_tok": round(a["avg_out"]),
                      "avg_dur_s": round(a["avg_dur"], 1)})
    if crows:
        with open(os.path.join(RESULTS_DIR, "complexity.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=ccols)
            w.writeheader()
            w.writerows(crows)
        ctab = [ccols] + [[str(r[c]) for c in ccols] for r in crows]
        cw = [max(len(row[i]) for row in ctab) for i in range(len(ccols))]
        print("\n  task complexity (relative 0-10 from observed effort, all models pooled):")
        for i, row in enumerate(ctab):
            print("  " + "  ".join(row[j].ljust(cw[j]) for j in range(len(ccols))))
            if i == 0:
                print("  " + "  ".join("-" * cw[j] for j in range(len(ccols))))


if __name__ == "__main__":
    main()
