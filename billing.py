#!/usr/bin/env python3
"""Pull the ACTUAL Modal bill for the GLM auto-endpoint, counting ONLY the time GLM actually ran.

The per-task numbers in the report are modeled (generation-seconds x rate). This is the ground-truth
counterpart: what Modal billed for the endpoint app — but prorated by the *union of self-hosted (GLM)
job windows*, NOT the whole manifest span. So hours where only Opus (a non-self-hosted API model) ran,
and idle gaps between GLM jobs (a crash-and-resume, or "GLM in the morning, Opus in the afternoon"),
correctly cost $0. Only the endpoint app is summed; each hour is prorated by how many seconds of it a
GLM job was actually in flight.

Run with a venv that has `modal` (and `modal setup` done):
    python3 billing.py --results-dir results/2026-...        # -> writes <dir>/billing.json

aggregate.py reads billing.json and shows the actual endpoint spend next to the modeled cost.
"""
import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone


def is_self_hosted(model):
    """GPU-billed = served via a modal provider (modal/, modal-high/, modal-nothink/)."""
    return (model or "").lower().split("/")[0].startswith("modal")


def glm_intervals(results_dir):
    """Absolute-epoch (start, end) windows of self-hosted (GLM) jobs only. Opus/API rows are excluded
    — the endpoint isn't up on their account."""
    ivs = []
    with open(os.path.join(results_dir, "manifest.csv")) as f:
        for r in csv.DictReader(f):
            if not is_self_hosted(r.get("model")):
                continue
            try:
                s, e = float(r["start"]), float(r["end"])
            except (TypeError, ValueError, KeyError):
                continue
            if e > s:
                ivs.append((s, e))
    return ivs


def merge(ivs):
    """Merge overlapping intervals into a sorted, disjoint union (so concurrent GLM jobs count the
    wall-clock once, not N times)."""
    out = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def covered_seconds(merged, a, b):
    """Seconds of [a, b] covered by the merged GLM-active intervals."""
    tot = 0.0
    for s, e in merged:
        lo, hi = max(a, s), min(b, e)
        if hi > lo:
            tot += hi - lo
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--app", default="ep-Modal-Auto-Endpoints",
                    help="billing app 'description' to sum [%(default)s]")
    ap.add_argument("--out", help="[<results-dir>/billing.json]")
    a = ap.parse_args()

    import modal  # imported here so the file at least parses without modal installed

    ivs = glm_intervals(a.results_dir)
    if not ivs:
        raise SystemExit(f"no GLM (self-hosted) job timestamps in {a.results_dir}/manifest.csv")
    merged = merge(ivs)
    s, e = merged[0][0], merged[-1][1]                 # overall GLM-active span (only for query bounds)
    start = datetime.fromtimestamp(s, tz=timezone.utc)
    end = datetime.fromtimestamp(e, tz=timezone.utc)
    q0 = start.replace(minute=0, second=0, microsecond=0)
    q1 = end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    # Modal 1.5+: workspace_billing_report() is deprecated → Workspace.billing.report()
    rep = modal.Workspace.from_context().billing.report(start=q0, end=q1, resolution="h")
    total, active_s, by_hour = 0.0, 0.0, []
    for it in rep:
        if it.description != a.app:
            continue                                   # only the endpoint app; skip grading/other apps
        h0 = it.interval_start
        if h0.tzinfo is None:
            h0 = h0.replace(tzinfo=timezone.utc)
        h1 = h0 + timedelta(hours=1)
        cov = covered_seconds(merged, h0.timestamp(), h1.timestamp())   # sec of this hour GLM was running
        if cov <= 0:
            continue                                   # Opus-only hour or idle gap -> $0, correctly
        frac = cov / 3600.0
        hour_cost = float(it.cost or 0)
        billed = hour_cost * frac                      # prorate the hour by GLM-active seconds only
        total += billed
        active_s += cov
        by_hour.append({"hour": h0.isoformat(), "hour_cost": round(hour_cost, 4),
                        "glm_seconds_in_hour": round(cov), "billed": round(billed, 4)})

    active_h = active_s / 3600.0
    out = {"app": a.app, "window_start": start.isoformat(), "window_end": end.isoformat(),
           "window_hours": round(active_h, 3),        # GLM-active hours (union), not the wall-clock span
           "cost": round(total, 4),
           "effective_hourly": round(total / active_h, 4) if active_h else None, "by_hour": by_hour}
    dst = a.out or os.path.join(a.results_dir, "billing.json")
    with open(dst, "w") as f:
        json.dump(out, f, indent=2)
    print(f"GLM endpoint ({a.app}) billed for GLM-active time only: ${total:.2f} over {active_h:.2f}h "
          f"active (~${out['effective_hourly']}/hr while serving) -> {dst}")


if __name__ == "__main__":
    main()
