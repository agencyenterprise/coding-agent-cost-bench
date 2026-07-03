#!/usr/bin/env python3
"""
Aggregate benchmark results -> results/{results_detailed,summary}.csv.

ONE cost per row, always labeled with its basis. Two currencies:
  * API models (Claude/GPT/Gemini) -> cost = tokens x per-token price (how the provider
    bills). From ccusage. basis "api_ccusage".
  * GLM on Modal (self-host)        -> you pay GPU TIME (billed even while idle). The honest
    number is the real Modal spend for the run window / successful tasks. basis "gpu_billing".
    Fetched automatically from modal.billing (needs: pip install modal && modal setup).

Inputs: results/manifest.csv + results/<outdir>/usage.json (ccusage session --json shape).
Self-host detection: any model served via the `modal/` provider (e.g. modal/zai-org/GLM-5.2-FP8).
"""
import csv
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

RESULTS_DIR = os.environ.get("RESULTS_DIR", "./results")
_GPU = ("gpu", "b200", "h200", "h100", "a100", "l40", "l4", "t4", "rtx")


def is_self_hosted(model):
    """Self-hosted (GPU-billed) = served via the modal/ provider."""
    return model.lower().startswith("modal/")


def load_usage(outdir):
    """(tokens_in, tokens_out, ccusage_cost) for a run from its usage.json."""
    try:
        with open(os.path.join(outdir, "usage.json")) as f:
            sessions = json.load(f).get("sessions", [])
    except Exception:
        return 0, 0, 0.0
    tin = sum(int(s.get("inputTokens", 0) or 0) for s in sessions)
    tout = sum(int(s.get("outputTokens", 0) or 0) for s in sessions)
    cost = sum(float(s.get("totalCost", 0) or 0) for s in sessions)
    return tin, tout, cost


def modal_window_usd(starts, ends):
    """Real Modal GPU $ for [min(starts), max(ends)] via modal.billing (ImportError if absent)."""
    from modal.billing import workspace_billing_report
    s = datetime.fromtimestamp(min(starts), timezone.utc)
    e = datetime.fromtimestamp(max(ends), timezone.utc)
    total = 0.0
    for item in workspace_billing_report(start=s, end=e, resolution="h", tag_names=["*"]):
        by_res = getattr(item, "cost_by_resource", None)
        if isinstance(by_res, dict):
            total += float(sum(v for k, v in by_res.items() if any(g in k.lower() for g in _GPU)))
        else:
            total += float(getattr(item, "cost", 0) or 0)
    return total


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    manifest = os.path.join(RESULTS_DIR, "manifest.csv")
    if not os.path.exists(manifest):
        sys.exit(f"no manifest at {manifest} — run run_bench.sh first")

    detailed = []
    times = defaultdict(lambda: ([], []))   # model -> (starts, ends)
    with open(manifest) as f:
        for row in csv.DictReader(f):
            m = row["model"]
            tin, tout, ccost = load_usage(row.get("outdir", ""))
            s, e = _f(row.get("start")), _f(row.get("end"))
            if s:
                times[m][0].append(s)
            if e:
                times[m][1].append(e)
            detailed.append({
                "task": row["task"], "model": m, "run": row["run"],
                "status": row["status"], "duration_s": row.get("duration_s", ""),
                "tokens_in": tin, "tokens_out": tout,
                "cost_usd": "" if is_self_hosted(m) else round(ccost, 6),
                "cost_basis": "gpu_amortized" if is_self_hosted(m) else "api_ccusage",
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
        row = {
            "model": model, "runs": n, "passes": passes,
            "success_rate": round(passes / n, 3),
            "avg_tokens_in": round(statistics.mean(r["tokens_in"] for r in runs)),
            "avg_tokens_out": round(statistics.mean(r["tokens_out"] for r in runs)),
            "avg_duration_s": round(statistics.mean(durs), 1) if durs else "",
            "total_cost_usd": "", "cost_per_successful_task": "", "cost_basis": "",
        }
        if is_self_hosted(model):
            starts, ends = times[model]
            if not (starts and ends):
                row["cost_basis"] = "gpu_pending (no timestamps)"
            else:
                try:
                    w_usd = modal_window_usd(starts, ends)
                    row["cost_basis"] = "gpu_billing"
                    row["total_cost_usd"] = round(w_usd, 4)
                    if passes:
                        row["cost_per_successful_task"] = round(w_usd / passes, 4)
                except ImportError:
                    row["cost_basis"] = "gpu_pending (pip install modal && modal setup)"
                except Exception as ex:
                    row["cost_basis"] = f"gpu_pending ({type(ex).__name__})"
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

    print("wrote results/results_detailed.csv + results/summary.csv\n")
    for r in rows:
        print(f"  {r['model']:<32} {r['passes']}/{r['runs']} pass  "
              f"{str(r['avg_duration_s']):<6}s  in={r['avg_tokens_in']:<6} out={r['avg_tokens_out']:<6} "
              f"total=${str(r['total_cost_usd'] or '—'):<8} $/task=${str(r['cost_per_successful_task'] or '—'):<8} [{r['cost_basis']}]")


if __name__ == "__main__":
    main()
