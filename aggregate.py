#!/usr/bin/env python3
"""
Aggregate benchmark results into a cost-per-task CSV.

Joins three sources per run:
  1. results/manifest.csv         -> which (task,model,run), status, duration
  2. per-run usage.json           -> tokens + $ from this run's isolated opencode DB
     legacy fallback: ccusage before/after diff in outdir, or global ccusage.NNNN snaps
  3. modal_costs.csv (optional)   -> real GPU $ for the self-hosted GLM/Modal model

Per-run usage.json is produced by run_bench.sh (ccusage against a temp HOME copy of
the job's isolated opencode.db). All sessions in that file belong to the run.

Two currencies (this is the crux of the study):
  - Claude / OpenAI  -> $ comes from ccusage (it knows per-token prices)
  - GLM on Modal     -> ccusage shows $0.00 (self-hosted). Real $ = GPU-seconds * rate,
                        supplied via modal_costs.csv (preferred) or duration * MODAL_RATE.
"""
import csv, json, os, sys, glob, statistics
from collections import defaultdict

RESULTS_DIR = os.environ.get("RESULTS_DIR", "./results")
MODAL_COSTS = os.environ.get("MODAL_COSTS", "./modal_costs.csv")
# Fallback only if modal_costs.csv is missing a row: $ = duration_s * MODAL_RATE_PER_SEC.
# NOTE: duration overestimates GPU time (includes harness/network). Prefer modal_costs.csv.
MODAL_RATE_PER_SEC = float(os.environ.get("MODAL_RATE_PER_SEC", "0") or 0)

# Models whose $ must come from Modal GPU billing, not ccusage. Match by substring.
GLM_MODELS = [m.strip() for m in os.environ.get(
    "GLM_MODELS", "modal,glm,big-pickle").split(",") if m.strip()]

# --- ccusage session --json schema: adjust these once you paste a real sample ---
SESSION_ID_KEYS = ["sessionId", "session", "id"]
INPUT_KEYS      = ["inputTokens", "input", "input_tokens", "inputTokensTotal"]
OUTPUT_KEYS     = ["outputTokens", "output", "output_tokens", "outputTokensTotal"]
COST_KEYS       = ["costUSD", "cost", "totalCost", "totalCostUSD"]
LIST_WRAPPER_KEYS = ["sessions", "data", "opencode"]  # if json is a dict, sessions live here


def _first(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def load_sessions(path):
    """Return dict: session_id -> {'in':int,'out':int,'cost':float}."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception:
        return {}
    if isinstance(raw, dict):
        rows = None
        for k in LIST_WRAPPER_KEYS:
            if isinstance(raw.get(k), list):
                rows = raw[k]; break
        rows = rows if rows is not None else [raw]
    else:
        rows = raw
    out = {}
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        sid = _first(r, SESSION_ID_KEYS, default=f"_idx{i}")
        out[str(sid)] = {
            "in":   int(_first(r, INPUT_KEYS, 0) or 0),
            "out":  int(_first(r, OUTPUT_KEYS, 0) or 0),
            "cost": float(_first(r, COST_KEYS, 0.0) or 0.0),
        }
    return out


def snap_path(n):
    return os.path.join(RESULTS_DIR, f"ccusage.{int(n):04d}.json")


def session_delta(before_path, after_path):
    """Return (new_session_ids, after_sessions_dict)."""
    before = load_sessions(before_path)
    after = load_sessions(after_path)
    new_ids = set(after) - set(before)
    return new_ids, after


def load_run_usage(outdir):
    """Return (session_ids, sessions_dict) for one benchmark run."""
    usage = os.path.join(outdir, "usage.json")
    if os.path.exists(usage):
        sess = load_sessions(usage)
        return set(sess.keys()), sess
    after = os.path.join(outdir, "ccusage.after.json")
    before = os.path.join(outdir, "ccusage.before.json")
    if os.path.exists(after):
        return session_delta(before, after)
    return set(), {}


def load_modal_costs():
    """Return dict: (task,model,run) -> gpu_cost_usd (or None)."""
    costs = {}
    if not os.path.exists(MODAL_COSTS):
        return costs
    with open(MODAL_COSTS) as f:
        for row in csv.DictReader(f):
            key = (row["task"], row["model"], str(row["run"]))
            if row.get("gpu_cost_usd"):
                costs[key] = float(row["gpu_cost_usd"])
            elif row.get("gpu_seconds") and MODAL_RATE_PER_SEC:
                costs[key] = float(row["gpu_seconds"]) * MODAL_RATE_PER_SEC
    return costs


def is_glm(model):
    return any(g in model for g in GLM_MODELS)


def _fmt_dur(seconds):
    s = float(seconds)
    return f"{s/60:.1f}m" if s >= 60 else f"{s:.0f}s"


def _fmt_int(n):
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1e6:.1f}M"
    if n >= 10_000:
        return f"{n/1e3:.0f}k"
    return str(n)


def main():
    manifest = os.path.join(RESULTS_DIR, "manifest.csv")
    if not os.path.exists(manifest):
        sys.exit(f"no manifest at {manifest} — run run_bench.sh first")

    modal_costs = load_modal_costs()
    detailed = []
    with open(manifest) as f:
        for row in csv.DictReader(f):
            outdir = row.get("outdir", "")
            if outdir:
                new_ids, sess = load_run_usage(outdir)
            elif row.get("snap_index", "").strip():
                snap = int(row["snap_index"])
                new_ids, sess = session_delta(snap_path(snap - 1), snap_path(snap))
            else:
                new_ids, sess = set(), {}
            tin  = sum(sess[s]["in"]   for s in new_ids)
            tout = sum(sess[s]["out"]  for s in new_ids)
            ccost = sum(sess[s]["cost"] for s in new_ids)

            key = (row["task"], row["model"], row["run"])
            if is_glm(row["model"]):
                gpu = modal_costs.get(key)
                if gpu is None and MODAL_RATE_PER_SEC:
                    gpu = float(row["duration_s"]) * MODAL_RATE_PER_SEC
                if gpu is not None:
                    cost_final, cost_src = gpu, "modal_gpu"          # real GPU spend (best)
                else:
                    cost_final, cost_src = ccost, "ccusage_estimate"  # fallback: hosted-price est., NOT GPU
            else:
                cost_final = ccost
                cost_src = "ccusage_tokens"

            detailed.append({
                "task": row["task"], "model": row["model"], "run": row["run"],
                "status": row["status"], "duration_s": row["duration_s"],
                "tokens_in": tin, "tokens_out": tout,
                "cost_tokens": round(ccost, 6), "cost_final": round(cost_final, 6),
                "cost_source": cost_src, "n_sessions": len(new_ids),
            })

    det_path = os.path.join(RESULTS_DIR, "results_detailed.csv")
    with open(det_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(detailed[0].keys()))
        w.writeheader(); w.writerows(detailed)

    # summary per model: success rate + $/successful-task (the number for James's graph)
    by_model = defaultdict(list)
    for d in detailed:
        by_model[d["model"]].append(d)

    sum_rows = []
    for model, runs in by_model.items():
        n = len(runs)
        passes = [r for r in runs if r["status"] == "pass"]
        fails = [r for r in runs if r["status"] == "fail"]
        total_cost = sum(r["cost_final"] for r in runs)
        pass_tokens_in = statistics.mean(r["tokens_in"] for r in passes) if passes else 0
        pass_tokens_out = statistics.mean(r["tokens_out"] for r in passes) if passes else 0
        sum_rows.append({
            "model": model,
            "runs": n,
            "passes": len(passes),
            "fails": len(fails),
            "success_rate": round(len(passes) / n, 3) if n else 0,
            "avg_tokens_in": round(statistics.mean(r["tokens_in"] for r in runs)) if n else 0,
            "avg_tokens_out": round(statistics.mean(r["tokens_out"] for r in runs)) if n else 0,
            "avg_tokens_in_pass": round(pass_tokens_in) if passes else None,
            "avg_tokens_out_pass": round(pass_tokens_out) if passes else None,
            "avg_duration_s": round(statistics.mean(float(r["duration_s"]) for r in runs), 1) if n else 0,
            "median_duration_s": round(statistics.median(float(r["duration_s"]) for r in runs), 1) if n else 0,
            "total_duration_s": round(sum(float(r["duration_s"]) for r in runs), 1),
            "total_cost_usd": round(total_cost, 4),
            "cost_per_successful_task": round(total_cost / len(passes), 4) if passes else None,
            "missing_gpu_cost": sum(1 for r in runs if r["cost_source"] == "MISSING"),
            "ccusage_estimate_runs": sum(1 for r in runs if r["cost_source"] == "ccusage_estimate"),
        })

    sum_path = os.path.join(RESULTS_DIR, "summary.csv")
    with open(sum_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(sum_rows[0].keys()))
        w.writeheader(); w.writerows(sum_rows)

    print(f"wrote {det_path}")
    print(f"wrote {sum_path}\n")
    for r in sorted(sum_rows, key=lambda x: (x["cost_per_successful_task"] is None,
                                             x["cost_per_successful_task"] or 0)):
        cpt = r["cost_per_successful_task"]
        warn_parts = []
        if r["missing_gpu_cost"]:
            warn_parts.append(f"{r['missing_gpu_cost']} missing GPU cost")
        if r["ccusage_estimate_runs"]:
            warn_parts.append(f"{r['ccusage_estimate_runs']} ccusage_estimate (not real GPU $)")
        warn = f"  ⚠ {', '.join(warn_parts)}" if warn_parts else ""
        rec = f"{r['passes']}/{r['runs']}"
        print(
            f"  {r['model']:<30} success={r['success_rate']:<5} ({rec}) "
            f"$/task={cpt if cpt is not None else 'n/a':<8} "
            f"total=${r['total_cost_usd']:<7} "
            f"in={_fmt_int(r['avg_tokens_in']):<5} out={_fmt_int(r['avg_tokens_out']):<5} "
            f"time={_fmt_dur(r['avg_duration_s']):<6}{warn}"
        )


if __name__ == "__main__":
    main()
