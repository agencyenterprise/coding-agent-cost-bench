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


def call_seconds(outdir):
    """Endpoint generation time for a run = sum over steps of (first response part - step_start),
    parsed from output.log (opencode JSON-lines). Each step is one call to the model; the time
    from step_start to its first tool_use/text part is the generation latency. Excludes the gaps
    between steps (local tool/script exec: pip, pytest, git, file I/O) where the GPU is idle."""
    log = os.path.join(outdir, "output.log")
    if not os.path.exists(log):
        return 0.0
    total_ms = 0
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
        t, ts = ev.get("type"), ev.get("timestamp")
        if t == "step_start":
            start, counted = ts, False
        elif t in ("tool_use", "text") and start is not None and not counted and ts is not None:
            total_ms += ts - start
            counted = True
        elif t == "step_finish":
            start = None
    return total_ms / 1000.0


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
    intervals = defaultdict(list)   # model -> [(start, end), ...] for self-hosted GPU costing
    with open(manifest) as f:
        for row in csv.DictReader(f):
            m = row["model"]
            tin, tout, ccost = load_usage(row.get("outdir", ""))
            s, e = _f(row.get("start")), _f(row.get("end"))
            if s is not None and e is not None:
                intervals[m].append((s, e))
            cs = call_seconds(row.get("outdir", ""))   # generation time — for every model (info for APIs)
            detailed.append({
                "task": row["task"], "model": m, "run": row["run"],
                "status": row["status"],
                "start": _iso(s), "end": _iso(e), "duration_s": row.get("duration_s", ""),
                "call_s": round(cs, 1),
                "tokens_in": tin, "tokens_out": tout,
                "cost_usd": "" if is_self_hosted(m) else round(ccost, 6),
                "cost_basis": "gpu_calls" if is_self_hosted(m) else "api_ccusage",
            })
    if not detailed:
        sys.exit("manifest has no rows")

    with open(os.path.join(RESULTS_DIR, "results_detailed.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(detailed[0].keys()))
        w.writeheader()
        w.writerows(detailed)

    by_model = defaultdict(list)
    for d in detailed:
        by_model[d["model"]].append(d)

    rows = []
    for model, runs in by_model.items():
        n = len(runs)
        passes = sum(1 for r in runs if r["status"] == "pass")
        durs = [_f(r["duration_s"]) for r in runs if _f(r["duration_s"]) is not None]
        active = union_seconds(intervals[model])                        # wall-clock union = sole-tenant uptime
        overlap = sum(e - s for s, e in intervals[model]) - active      # time compressed by parallelism
        call_total = sum(v for r in runs if (v := _f(r.get("call_s"))) is not None)
        row = {
            "model": model, "runs": n, "passes": passes,
            "success_rate": round(passes / n, 3),
            "avg_tokens_in": round(statistics.mean(r["tokens_in"] for r in runs)),
            "avg_tokens_out": round(statistics.mean(r["tokens_out"] for r in runs)),
            "avg_duration_s": round(statistics.mean(durs), 1) if durs else "",
            "call_s": round(call_total, 1) if call_total else "",
            "active_s": round(active, 1) if active else "",
            "overlap_s": round(overlap, 1) if intervals[model] else "",
            "total_cost_usd": "", "cost_per_successful_task": "", "cost_basis": "",
        }
        if is_self_hosted(model):
            if call_total:
                cost = call_total / 3600 * GPU_HOURLY_USD   # billed on generation time only
                row["total_cost_usd"] = round(cost, 4)
                row["cost_per_successful_task"] = round(cost / passes, 4) if passes else ""
                row["cost_basis"] = "gpu_calls"
            else:
                row["cost_basis"] = "gpu_calls (no log timing)"
        else:
            total = sum(r["cost_usd"] for r in runs if r["cost_usd"] != "")
            row["total_cost_usd"] = round(total, 4)
            row["cost_per_successful_task"] = round(total / passes, 4) if passes else ""
            row["cost_basis"] = "api_ccusage"
        rows.append(row)

    with open(os.path.join(RESULTS_DIR, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    def _show(v):
        return "—" if v in ("", None) else str(v)

    print("wrote results/results_detailed.csv + results/summary.csv")
    print(f"(GLM GPU rate: ${GPU_HOURLY_USD:.2f}/hr, charged on endpoint call time only)\n")
    headers = ["model", "pass", "succ", "avg_s", "call_s", "uptime_s", "overlap_s",
               "tok_in", "tok_out", "$/task", "basis"]
    table = [headers]
    for r in rows:
        table.append([
            r["model"],
            f"{r['passes']}/{r['runs']}",
            f"{r['success_rate']:.0%}",
            _show(r["avg_duration_s"]),
            _show(r["call_s"]),
            _show(r["active_s"]),
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


if __name__ == "__main__":
    main()
