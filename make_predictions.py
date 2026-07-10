#!/usr/bin/env python3
"""Extract model patches from a bench results dir into a SWE-bench predictions.jsonl.

For every SWE-bench task run (task dir name contains 'swebench'), read the run's final_repo/ and
take `git diff HEAD` — the agent's fix relative to the post-setup baseline bench.sh commits — as the
`model_patch`. One prediction per (instance, harness, model, prompt, run), with a UNIQUE
`model_name_or_path` so the official SWE-bench harness scores every run independently.

The extra `_*` fields are metadata for mapping results back to the arm/prompt/run in our report;
the SWE-bench harness ignores unknown keys.

Usage (from repo root):
    python3 make_predictions.py --results-dir results/aep
    python3 make_predictions.py --results-dir results/aep --out results/aep/predictions.jsonl

Then evaluate on Modal (x86):
    python3 swe_eval_modal.py --predictions results/aep/predictions.jsonl
"""
import argparse
import csv
import json
import os
import subprocess


def instance_id(task):
    """demo-swebench-psf__requests-6028 -> psf__requests-6028."""
    return task.split("demo-swebench-", 1)[-1]


def resolve_outdir(results_dir, outdir):
    """Manifests store the outdir as it was at run time (often absolute); re-anchor to results_dir
    by basename if that path no longer exists (results copied/moved)."""
    if outdir and os.path.isdir(outdir):
        return outdir
    return os.path.join(results_dir, os.path.basename(outdir.rstrip("/")))


def model_patch(repo):
    """The agent's fix = `git diff HEAD` in final_repo (bench.sh committed a post-setup baseline that
    already has the test patch applied, so this diff is the fix only — exactly what SWE-bench wants)."""
    if not os.path.isdir(os.path.join(repo, ".git")):
        return ""
    r = subprocess.run(["git", "-C", repo, "diff", "HEAD"], capture_output=True, text=True)
    return r.stdout


def _safe(s):
    return s.replace("/", "_").replace(":", "_").replace(" ", "_")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", required=True, help="a bench results dir (contains manifest.csv)")
    ap.add_argument("--out", help="output predictions.jsonl [<results-dir>/predictions.jsonl]")
    a = ap.parse_args()

    manifest = os.path.join(a.results_dir, "manifest.csv")
    if not os.path.exists(manifest):
        raise SystemExit(f"no manifest.csv in {a.results_dir}")
    out = a.out or os.path.join(a.results_dir, "predictions.jsonl")

    preds, empty, missing = [], 0, 0
    with open(manifest) as f:
        for row in csv.DictReader(f):
            task = row.get("task", "")
            if "swebench" not in task.lower():
                continue
            repo = os.path.join(resolve_outdir(a.results_dir, row.get("outdir", "")), "final_repo")
            if not os.path.isdir(repo):
                missing += 1
                continue
            patch = model_patch(repo)
            if not patch.strip():
                empty += 1
            mnp = f"{row['harness']}__{_safe(row['model'])}__{row['prompt']}__run{row['run']}"
            preds.append({
                "instance_id": instance_id(task),
                "model_patch": patch,
                "model_name_or_path": mnp,
                "_task": task, "_harness": row["harness"], "_model": row["model"],
                "_prompt": row["prompt"], "_run": row["run"], "_status_host": row.get("status", ""),
            })

    with open(out, "w") as f:
        for p in preds:
            f.write(json.dumps(p) + "\n")
    insts = sorted({p["instance_id"] for p in preds})
    print(f"wrote {len(preds)} predictions across {len(insts)} instance(s) -> {out}")
    print(f"  instances: {', '.join(insts)}")
    if empty:
        print(f"  ⚠️ {empty} prediction(s) had an EMPTY patch (agent made no diff) — will score unresolved")
    if missing:
        print(f"  ⚠️ {missing} run(s) had no final_repo/ (run with KEEP_REPO=1 / default) — skipped")


if __name__ == "__main__":
    main()
