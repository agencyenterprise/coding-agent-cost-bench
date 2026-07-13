#!/usr/bin/env python3
"""Render the live/final run table for bench.sh: harness/model, calls_s, tools_s, elapsed.

Reads a growing TSV (one row per finished run: harness, model, task, start, end, calls_s) and a
setup-order file (harness<TAB>model lines, the harness/model order to group by). Per run:
  calls_s   = API generation time (step_start..step_finish, from the log)
  tools_s   = elapsed - calls_s   (local tool exec, git, retries, scheduling gaps)
  elapsed   = end - start          (wall clock for that run)
Per-setup subtotal: calls_s and tools_s SUM (additive compute), but elapsed is the WALL SPAN
(max end - min start) of that setup's runs, since runs overlap and summing would overcount.

    python3 runtable.py --tsv results/<run>/runtable.tsv --order results/<run>/.setups.order \
        [--total 40] [--running 4]
"""
import argparse
import csv
import os

TAG = {"modal": "", "modal-high": " (high)", "modal-nothink": " (no-think)"}
W = (38, 10, 10, 10)


def label(model):
    m = model.split("/")[-1]
    head = model.split("/")[0]
    return m + TAG.get(head, "")


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def row(cells, indent=""):
    c0 = (indent + cells[0])[: W[0]]
    return "  ".join([c0.ljust(W[0])] + [str(cells[i]).rjust(W[i]) for i in range(1, 4)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tsv", required=True)
    ap.add_argument("--order", required=True)
    ap.add_argument("--total", type=int, default=0)
    ap.add_argument("--running", type=int, default=0)
    a = ap.parse_args()

    order = []
    if os.path.exists(a.order):
        for ln in open(a.order):
            p = ln.rstrip("\n").split("\t")
            if len(p) == 2:
                order.append((p[0], p[1]))

    rows = []
    if os.path.exists(a.tsv):
        with open(a.tsv) as f:
            for r in csv.reader(f, delimiter="\t"):
                if len(r) >= 6:
                    rows.append(r)  # harness, model, task, start, end, calls_s

    groups = {}
    for h, m, task, s, e, calls in rows:
        groups.setdefault((h, m), []).append((task, fnum(s), fnum(e), fnum(calls)))
    keys = order + [k for k in groups if k not in order]

    done = len(rows)
    out = []
    prog = f"  ({done}/{a.total} done" + (f", {a.running} running" if a.running else "") + ")" if a.total else ""
    out.append(f"run table{prog}")
    out.append(row(("harness / model", "calls_s", "tools_s", "elapsed")))
    out.append("-" * (W[0] + sum(W[1:]) + 6))

    g_calls = g_tools = 0.0
    g_start, g_end = None, None
    for k in keys:
        runs = groups.get(k)
        if not runs:
            continue
        sc = st = 0.0
        smin = smax = None
        for task, s, e, calls in sorted(runs, key=lambda x: (x[1] or 0)):
            elapsed = (e - s) if (s and e) else 0.0
            tools = max(0.0, elapsed - (calls or 0.0))
            sc += calls or 0.0
            st += tools
            if s:
                smin = s if smin is None else min(smin, s)
            if e:
                smax = e if smax is None else max(smax, e)
            t = task.replace("demo-swebench-", "")
            out.append(row((f"{label(k[1])} · {t}", f"{calls or 0:.0f}", f"{tools:.0f}", f"{elapsed:.0f}")))
        span = (smax - smin) if (smin and smax) else 0.0
        out.append(row((f"= {k[0]} / {label(k[1])}  ({len(runs)} runs)",
                        f"{sc:.0f}", f"{st:.0f}", f"{span:.0f}")))
        out.append("")
        g_calls += sc
        g_tools += st
        if smin:
            g_start = smin if g_start is None else min(g_start, smin)
        if smax:
            g_end = smax if g_end is None else max(g_end, smax)

    g_span = (g_end - g_start) if (g_start and g_end) else 0.0
    out.append("-" * (W[0] + sum(W[1:]) + 6))
    out.append(row(("TOTAL", f"{g_calls:.0f}", f"{g_tools:.0f}", f"{g_span:.0f}")))
    print("\n".join(out))


if __name__ == "__main__":
    main()
