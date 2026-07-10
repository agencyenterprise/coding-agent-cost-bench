#!/usr/bin/env python3
"""Pull the ACTUAL Modal bill for the auto-endpoint during a benchmark run's window.

The per-task numbers in the report are modeled (generation-seconds x rate). This is the ground-truth
counterpart: what Modal actually billed for the AUTO-ENDPOINT app during the wall-clock window the run
occupied. Only that one app is summed (not swe-bench-eval grading, not other workspace apps), and only
for the run's hours (edge hours are prorated by how much of the hour the run actually spanned).

Run with a venv that has `modal` (and `modal setup` done):
    python3 billing.py --results-dir results/aep-...          # -> writes <dir>/billing.json
    python3 billing.py --results-dir results/app-... --app glm-5-2-app-benchmark   # the custom App

aggregate.py reads billing.json and shows the actual endpoint spend next to the modeled cost.
"""
import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone


def run_window(results_dir):
    """(start, end) epoch seconds spanning all runs in the manifest (bench.sh writes epoch start/end)."""
    starts, ends = [], []
    with open(os.path.join(results_dir, "manifest.csv")) as f:
        for r in csv.DictReader(f):
            try:
                starts.append(float(r["start"]))
                ends.append(float(r["end"]))
            except (TypeError, ValueError):
                pass
    if not starts:
        raise SystemExit(f"no run timestamps in {results_dir}/manifest.csv")
    return min(starts), max(ends)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--app", default="ep-Modal-Auto-Endpoints",
                    help="billing app 'description' to sum [%(default)s] (custom App: glm-5-2-app-benchmark)")
    ap.add_argument("--out", help="[<results-dir>/billing.json]")
    a = ap.parse_args()

    import modal.billing as mb  # imported here so the file at least parses without modal installed

    s, e = run_window(a.results_dir)
    start = datetime.fromtimestamp(s, tz=timezone.utc)
    end = datetime.fromtimestamp(e, tz=timezone.utc)
    q0 = start.replace(minute=0, second=0, microsecond=0)
    q1 = end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    rep = mb.workspace_billing_report(start=q0, end=q1, resolution="h")
    total, by_hour = 0.0, []
    for it in rep:
        d = dict(it)
        if d.get("description") != a.app:
            continue                                   # only the endpoint app; skip grading/other apps
        h0 = d["interval_start"]
        if h0.tzinfo is None:
            h0 = h0.replace(tzinfo=timezone.utc)
        h1 = h0 + timedelta(hours=1)
        overlap = (min(end, h1) - max(start, h0)).total_seconds()   # seconds of this hour inside the run
        if overlap <= 0:
            continue
        frac = overlap / 3600.0
        hour_cost = float(d.get("cost") or 0)
        billed = hour_cost * frac                      # prorate the edge hours by overlap
        total += billed
        by_hour.append({"hour": h0.isoformat(), "hour_cost": round(hour_cost, 4),
                        "fraction_in_run": round(frac, 3), "billed": round(billed, 4)})

    window_h = (end - start).total_seconds() / 3600.0
    out = {"app": a.app, "window_start": start.isoformat(), "window_end": end.isoformat(),
           "window_hours": round(window_h, 3), "cost": round(total, 4),
           "effective_hourly": round(total / window_h, 4) if window_h else None, "by_hour": by_hour}
    dst = a.out or os.path.join(a.results_dir, "billing.json")
    with open(dst, "w") as f:
        json.dump(out, f, indent=2)
    print(f"AEP ({a.app}) billed during the run window: ${total:.2f} over {window_h:.2f}h "
          f"(~${out['effective_hourly']}/hr) -> {dst}")


if __name__ == "__main__":
    main()
