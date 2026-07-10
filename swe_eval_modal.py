#!/usr/bin/env python3
"""Evaluate SWE-bench predictions on Modal (x86) using the official per-instance images.

No docker-in-docker: every Verified instance has a prebuilt eval image on DockerHub
(swebench/sweb.eval.x86_64.<id with __ -> _1776_>). We run each as a Modal *Sandbox* (Modal pulls
the x86 image natively), apply the model_patch in /testbed, run the instance's own eval_script
(pip install the patched source, reset+apply the test patch, pytest), and grade the captured test
output with SWE-bench's own per-repo parser. Writes resolved.json keyed by model_name_or_path.

Prereqs (in a venv): `pip install modal swebench pyarrow`, then `modal setup` once for auth. The
SWE-bench_Verified parquet must be in the HF cache (make_swebench_task.py / make_predictions.py path).

    python3 swe_eval_modal.py --predictions results/aep/predictions.jsonl
    python3 swe_eval_modal.py --predictions ... --only psf__requests-6028 --limit 1   # smoke test
    python3 swe_eval_modal.py --predictions ... --status-host pass                     # only host-passing runs
"""
import argparse
import base64
import collections
import glob
import json
import os

import modal
import pyarrow.parquet as pq
from swebench.harness.test_spec.test_spec import make_test_spec
from swebench.harness.log_parsers import MAP_REPO_TO_PARSER

START, END = ">>>>> Start Test Output", ">>>>> End Test Output"


def load_instances():
    rows = {}
    for p in glob.glob(os.path.expanduser(
            "~/.cache/huggingface/hub/datasets--princeton-nlp--SWE-bench_Verified/snapshots/*/data/*.parquet")):
        for r in pq.read_table(p).to_pylist():
            rows[r["instance_id"]] = r
    if not rows:
        raise SystemExit("SWE-bench_Verified parquet not in the HF cache — download it first.")
    return rows


def dockerhub(image_key):
    """sweb.eval.x86_64.psf__requests-6028:latest -> swebench/sweb.eval.x86_64.psf_1776_requests-6028:latest"""
    return "swebench/" + image_key.replace("__", "_1776_")


def as_list(x):
    return x if isinstance(x, list) else json.loads(x)


def grade(repo, output, f2p, p2p):
    """Parse the test output between SWE-bench's markers with the repo's parser; resolved iff every
    FAIL_TO_PASS and every PASS_TO_PASS test PASSED."""
    seg = output.split(START, 1)[-1].split(END, 1)[0] if START in output else output
    parser = MAP_REPO_TO_PARSER[repo]
    try:
        status = parser(seg)
    except TypeError:
        status = parser(seg, None)   # newer parsers take (log, test_spec)
    ok = lambda t: status.get(t) == "PASSED"
    resolved = bool(f2p) and all(ok(t) for t in f2p) and all(ok(t) for t in p2p)
    summary = {"f2p_pass": sum(ok(t) for t in f2p), "f2p_total": len(f2p),
               "p2p_pass": sum(ok(t) for t in p2p), "p2p_total": len(p2p)}
    return resolved, summary


def run_one(app, image, eval_script, patch, repo, f2p, p2p):
    sb = modal.Sandbox.create(app=app, image=image, cpu=4.0, memory=8192, timeout=1800)
    try:
        b64p = base64.b64encode(patch.encode()).decode()
        b64s = base64.b64encode(eval_script.encode()).decode()
        sb.exec("bash", "-lc",
                f"echo {b64p} | base64 -d > /tmp/model.patch && echo {b64s} | base64 -d > /tmp/eval.sh").wait()
        ap = sb.exec("bash", "-lc",
                     "cd /testbed && (git apply -v /tmp/model.patch || git apply --3way /tmp/model.patch "
                     "|| patch -p1 -i /tmp/model.patch) 2>&1")
        apply_out = ap.stdout.read(); ap.wait()
        ev = sb.exec("bash", "-lc", "bash /tmp/eval.sh 2>&1")
        output = ev.stdout.read(); ev.wait()
    finally:
        sb.terminate()
    resolved, summary = grade(repo, output, f2p, p2p)
    return resolved, summary, apply_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--out", help="[<pred dir>/resolved.json]")
    ap.add_argument("--only", default="", help="only this instance_id")
    ap.add_argument("--limit", type=int, default=0, help="max predictions per instance (0 = all)")
    ap.add_argument("--status-host", default="", help="only predictions whose host status matches (e.g. pass)")
    ap.add_argument("--app", default="swe-bench-eval")
    a = ap.parse_args()
    out = a.out or os.path.join(os.path.dirname(a.predictions) or ".", "resolved.json")

    preds = [json.loads(ln) for ln in open(a.predictions) if ln.strip()]
    if a.only:
        preds = [p for p in preds if p["instance_id"] == a.only]
    if a.status_host:
        preds = [p for p in preds if p.get("_status_host") == a.status_host]
    by_inst = collections.defaultdict(list)
    for p in preds:
        by_inst[p["instance_id"]].append(p)

    inst_rows = load_instances()
    app = modal.App.lookup(a.app, create_if_missing=True)
    results = {}
    for iid, plist in by_inst.items():
        if a.limit:
            plist = plist[:a.limit]
        row = inst_rows[iid]
        ts = make_test_spec(row)
        f2p, p2p = as_list(row["FAIL_TO_PASS"]), as_list(row["PASS_TO_PASS"])
        image = modal.Image.from_registry(dockerhub(ts.instance_image_key))
        print(f"=== {iid}: {len(plist)} prediction(s) -> {dockerhub(ts.instance_image_key)}")
        for p in plist:
            mnp, patch = p["model_name_or_path"], p.get("model_patch", "")
            # the same model_name_or_path recurs for every instance, so key by instance::mnp
            rkey = f"{iid}::{mnp}"
            if not patch.strip():
                results[rkey] = {"instance_id": iid, "model_name_or_path": mnp, "resolved": False, "note": "empty patch"}
                print(f"  {mnp}: empty patch -> unresolved")
                continue
            try:
                resolved, summary, _ = run_one(app, image, ts.eval_script, patch, row["repo"], f2p, p2p)
                results[rkey] = {"instance_id": iid, "model_name_or_path": mnp, "resolved": resolved, **summary}
                print(f"  {mnp}: {'RESOLVED' if resolved else 'unresolved'} "
                      f"(f2p {summary['f2p_pass']}/{summary['f2p_total']}, p2p {summary['p2p_pass']}/{summary['p2p_total']})")
            except Exception as e:
                results[rkey] = {"instance_id": iid, "model_name_or_path": mnp, "resolved": False, "error": str(e)[:200]}
                print(f"  {mnp}: ERROR {e}")

    json.dump(results, open(out, "w"), indent=2)
    n = sum(1 for r in results.values() if r.get("resolved"))
    print(f"\nwrote {out}: {n}/{len(results)} resolved")


if __name__ == "__main__":
    main()
