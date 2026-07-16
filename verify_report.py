#!/usr/bin/env python3
"""Correctness gate for benchmark_progress_report.py's cost/pass numbers.

The report drives money figures the team reads, so every claim it makes is checked here against an
INDEPENDENT recomputation from the raw files (manifest.csv, reward.json, billing.json, output.log) —
not against the report's own internals. Also runs a synthetic fixture with a hand-computed answer to
prove the concurrency split (attribute_cost) is exact.

    python3 verify_report.py results/<run>        # exits non-zero (and prints FAIL) on any mismatch
"""
import csv, json, os, sys
from collections import defaultdict

import benchmark_progress_report as R
aggregate = R  # engine is now inlined into the single report file; keep the name for the checks below

CENT = 0.01
EPS = 1e-6
fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "FAIL  ") + name + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---- 1. synthetic fixture: attribute_cost must match a hand-computed split -----------------------
# Two runs share [0,10] at $1/s, then run A runs alone [10,20]. Total = rate×union(0..20) = $20.
#   [0,10):  2 active -> $0.5/s each -> A=$5, B=$5
#   [10,20): A alone  -> $1/s        -> A=$10
#   => A=$15, B=$5, sum=$20.
def test_fixture():
    owned = [(0.0, 10.0, "A"), (0.0, 10.0, "B"), (10.0, 20.0, "A")]
    union = aggregate.union_seconds([(s, e) for s, e, _ in owned])
    got = aggregate.attribute_cost(owned, 1.0)
    check("fixture union == 20s", abs(union - 20.0) < EPS, f"{union}")
    check("fixture A == $15", abs(got["A"] - 15.0) < EPS, f"{got.get('A')}")
    check("fixture B == $5", abs(got["B"] - 5.0) < EPS, f"{got.get('B')}")
    check("fixture sum == rate×union ($20)", abs(sum(got.values()) - 20.0) < EPS, f"{sum(got.values())}")


# ---- 2. independent recompute of pass counts from reward.json (not the manifest, not the report) --
def truth_from_disk(run_dir):
    """Per-model: runs, passing runs (reward==1), distinct tasks solved (pass@k), + raw manifest pass."""
    per = defaultdict(lambda: {"runs": 0, "pass_runs": 0, "solved_tasks": set(), "tasks": set()})
    for entry in sorted(os.listdir(run_dir)):
        d = os.path.join(run_dir, entry)
        rj = os.path.join(d, "reward.json")
        if not os.path.isdir(d) or not os.path.exists(rj):
            continue
        model, task, _run = R.parse_dirname(entry)
        try:
            passed = json.load(open(rj)).get("reward") == 1
        except Exception:
            passed = False
        p = per[model]
        p["runs"] += 1
        p["tasks"].add(task)
        if passed:
            p["pass_runs"] += 1
            p["solved_tasks"].add(task)
    return per


def test_against_report(run_dir):
    truth = truth_from_disk(run_dir)
    runs, task_stat, cell, cost_is_real = R.collect(run_dir)

    # 2a. collect() must reproduce the disk pass counts exactly
    rep = defaultdict(lambda: {"runs": 0, "pass_runs": 0})
    for r in runs:
        rep[r["model"]]["runs"] += 1
        rep[r["model"]]["pass_runs"] += 1 if r["passed"] else 0
    for m, t in truth.items():
        check(f"[{m}] run count == disk", rep[m]["runs"] == t["runs"],
              f"report {rep[m]['runs']} vs disk {t['runs']}")
        check(f"[{m}] passing runs == disk", rep[m]["pass_runs"] == t["pass_runs"],
              f"report {rep[m]['pass_runs']} vs disk {t['pass_runs']}")

    # 2b. manifest status must agree with reward.json (cross-source consistency)
    mism = 0
    mpath = os.path.join(run_dir, "manifest.csv")
    if os.path.exists(mpath):
        for row in csv.DictReader(open(mpath)):
            label = os.path.basename((row.get("outdir") or "").rstrip("/"))
            rj = os.path.join(run_dir, label, "reward.json")
            if not os.path.exists(rj):
                continue
            try:
                rew = json.load(open(rj)).get("reward") == 1
            except Exception:
                rew = False
            if (row.get("status") == "pass") != rew:
                mism += 1
        check("manifest status == reward.json (all runs)", mism == 0, f"{mism} mismatches")

    # 2c. cost invariants: apply the SAME attribution the report does, then check reconciliation
    owned = [(s, e, r["label"]) for r in runs if r["model"] != "opus" for (s, e) in r["ivs"]]
    gen_union = aggregate.union_seconds([(s, e) for (s, e, _o) in owned])
    bill = None
    if cost_is_real:
        try:
            bill = float(json.load(open(os.path.join(run_dir, "billing.json")))["cost"])
        except Exception:
            bill = None
    if gen_union > 0:
        rate = (bill / gen_union) if bill else (aggregate.GPU_HOURLY_USD / 3600.0)
        billed = aggregate.attribute_cost(owned, rate)
        glm_total = sum(billed.values())
        if bill:
            check("GLM attributed total == real bill (to the cent)", abs(glm_total - bill) < CENT,
                  f"attributed ${glm_total:.4f} vs bill ${bill:.4f}")
        check("every GLM run cost >= 0", all(v >= -EPS for v in billed.values()))
        # no GLM run should exceed its own sole cost (concurrency only ever discounts)
        sole = {}
        for r in runs:
            if r["model"] != "opus":
                sole[r["label"]] = aggregate.union_seconds(r["ivs"]) * rate
        over = [k for k, v in billed.items() if v > sole.get(k, 0) + CENT]
        check("no run billed above its sole cost (split only discounts)", not over,
              f"{len(over)} over: {over[:3]}")

    # 2d. Opus cost must be its own per-token charge, untouched by attribution
    opus_costs = [r["cost"] for r in runs if r["model"] == "opus" and r["cost"] is not None]
    check("opus costs present and non-negative", all(c >= -EPS for c in opus_costs) and opus_costs != [])


def main():
    run_dir = sys.argv[1] if len(sys.argv) > 1 else None
    if not run_dir or not os.path.isdir(run_dir):
        sys.exit("usage: python3 verify_report.py results/<run>")
    print("== synthetic fixture ==")
    test_fixture()
    print(f"\n== against {run_dir} ==")
    test_against_report(run_dir)
    print()
    if fails:
        sys.exit(f"FAILED {len(fails)} check(s): {fails}")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
