#!/usr/bin/env python3
"""Evaluate SWE-bench PRO predictions on Modal (x86) using the official per-instance images.

Counterpart of swe_eval_modal.py (Verified) for tasks made by make_swebenchpro_task.py.
Everything an instance needs is vendored in its task dir (no dataset / scaleapi clone needed):
  meta.json   — instance_id, dockerhub_tag, base_commit ref (repo.git), before_repo_set_cmd,
                selected_test_files_to_run
  f2p.txt / pro_eval/{run_script.sh, parser.py, env.sh, p2p.json} — official grading assets

Per prediction: Modal Sandbox on jefzda/sweap-images:<tag> -> apply model_patch in /app at
base_commit -> checkout the dataset's test files (last line of before_repo_set_cmd) -> run the
instance's official run_script.sh on selected_test_files_to_run -> parse with its parser.py ->
resolved iff every FAIL_TO_PASS and PASS_TO_PASS test PASSED (Scale's exact criterion).
MERGES results into resolved.json (keyed instance::model_name_or_path), so it composes with the
Verified grader writing to the same file. Predictions whose instance has no Pro task dir are
skipped (they're Verified's).

    python3 swe_pro_eval_modal.py --predictions results/aep/predictions.jsonl
    python3 swe_pro_eval_modal.py --gold                # validate the gold patches of all Pro tasks
    python3 swe_pro_eval_modal.py --gold --only instance_navidrome__navidrome-5001518...
"""
import argparse
import collections
import glob
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import modal

DOCKERHUB_USER = "jefzda"          # Scale's public mirror of the per-instance eval images
JSONL = ".cache/SWE-bench_Pro-os/helper_code/sweap_eval_full_v2.jsonl"   # only needed for --gold


def load_tasks(tasks_dir):
    """instance_id -> everything vendored in its tasks/demo-swebenchpro-*/ dir."""
    tasks = {}
    for mp in glob.glob(os.path.join(tasks_dir, "*", "meta.json")):
        d = os.path.dirname(mp)
        meta = json.load(open(mp))
        if meta.get("benchmark") != "swebench_pro":
            continue
        ev = os.path.join(d, "pro_eval")
        if not os.path.isdir(ev):
            print(f"WARNING: {d} has no pro_eval/ (regenerate with make_swebenchpro_task.py) — skipped")
            continue
        base = open(os.path.join(d, "repo.git")).read().split()[1]
        sel = meta["selected_test_files_to_run"]
        if isinstance(sel, str):
            sel = json.loads(sel)
        tasks[meta["instance_id"]] = {
            "base_commit": base,
            "image": f"{DOCKERHUB_USER}/sweap-images:{meta['dockerhub_tag']}",
            "checkout_tests": meta["before_repo_set_cmd"].strip().split("\n")[-1],
            "selected": ",".join(sel),
            "f2p": [ln for ln in open(os.path.join(d, "f2p.txt")).read().splitlines() if ln],
            "p2p": json.load(open(os.path.join(ev, "p2p.json"))),
            "files": {name: open(os.path.join(ev, name)).read()
                      for name in ("run_script.sh", "parser.py", "env.sh")},
        }
    return tasks


def entryscript(t):
    """Scale's entryscript (swe_bench_pro_eval.py::create_entryscript), with our patch-apply
    fallbacks. No `set -e`: even a failed apply must still produce output.json (=> unresolved)."""
    return f"""source /workspace/env.sh || true
cd /app
git reset --hard {t['base_commit']}
git clean -fd
git checkout {t['base_commit']}
git apply -v /workspace/patch.diff || git apply --3way /workspace/patch.diff || patch -p1 -N -i /workspace/patch.diff
{t['checkout_tests']}
bash /workspace/run_script.sh {t['selected']} > /workspace/stdout.log 2> /workspace/stderr.log
python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json \\
  || python3 /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json
"""


def grade(output, f2p, p2p):
    passed = {x["name"] for x in output["tests"] if x["status"] == "PASSED"}
    ok = lambda t: t in passed
    resolved = bool(f2p) and all(ok(t) for t in f2p) and all(ok(t) for t in p2p)
    return resolved, {"f2p_pass": sum(ok(t) for t in f2p), "f2p_total": len(f2p),
                      "p2p_pass": sum(ok(t) for t in p2p), "p2p_total": len(p2p)}


def _eval_once(app, image, t, patch):
    sb = modal.Sandbox.create(app=app, image=image, cpu=(1, 4), memory=(5 * 1024, 30 * 1024),
                              timeout=3600)
    try:
        sb.exec("mkdir", "-p", "/workspace").wait()
        for name, content in [*t["files"].items(), ("patch.diff", patch),
                              ("entryscript.sh", entryscript(t))]:
            with sb.open(f"/workspace/{name}", "w") as f:
                f.write(content)
        sb.exec("bash", "/workspace/entryscript.sh").wait()
        try:
            with sb.open("/workspace/output.json", "r") as f:
                output = json.loads(f.read())
        except Exception as e:  # no output.json -> the eval itself never ran (infra) -> retryable
            tail = ""
            try:
                with sb.open("/workspace/stderr.log", "r") as f:
                    tail = f.read()[-300:]
            except Exception:
                pass
            raise RuntimeError(f"no output.json — eval did not run. stderr tail: {tail}") from e
    finally:
        sb.terminate()
    return grade(output, t["f2p"], t["p2p"])


def run_one(app, image, t, patch, retries=2):
    """Retries ONLY on infra errors (dead sandbox / eval that never produced output.json) —
    never on a legitimate 'unresolved' (tests ran and failed)."""
    last = None
    for _ in range(retries + 1):
        try:
            return _eval_once(app, image, t, patch)
        except Exception as e:  # noqa: BLE001 — transient Modal/sandbox/network failure; retry
            last = e
    raise last


def gold_predictions(tasks, jsonl=JSONL):
    if not os.path.exists(jsonl):
        raise SystemExit(f"--gold needs {jsonl} — git clone --depth 1 "
                         "https://github.com/scaleapi/SWE-bench_Pro-os .cache/SWE-bench_Pro-os")
    preds = []
    for ln in open(jsonl):
        r = json.loads(ln)
        if r["instance_id"] in tasks:
            preds.append({"instance_id": r["instance_id"], "model_patch": r["patch"],
                          "model_name_or_path": "gold"})
    return preds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", help="predictions.jsonl from make_predictions.py")
    ap.add_argument("--gold", action="store_true", help="grade the DATASET's gold patches instead")
    ap.add_argument("--out", help="[<pred dir>/resolved.json — merged, not overwritten]")
    ap.add_argument("--only", default="", help="only this instance_id (substring ok)")
    ap.add_argument("--limit", type=int, default=0, help="max predictions per instance (0 = all)")
    ap.add_argument("--status-host", default="", help="only predictions whose host status matches")
    ap.add_argument("--tasks-dir", default="tasks")
    ap.add_argument("--app", default="swe-bench-pro-eval")
    ap.add_argument("--workers", type=int, default=8, help="concurrent Modal sandboxes [%(default)s]")
    a = ap.parse_args()
    if not a.predictions and not a.gold:
        ap.error("need --predictions or --gold")

    tasks = load_tasks(a.tasks_dir)
    if a.gold:
        preds = gold_predictions(tasks)
        out = a.out or "resolved_gold_pro.json"
    else:
        preds = [json.loads(ln) for ln in open(a.predictions) if ln.strip()]
        out = a.out or os.path.join(os.path.dirname(a.predictions) or ".", "resolved.json")

    known = [p for p in preds if p["instance_id"] in tasks]
    if a.only:
        known = [p for p in known if a.only in p["instance_id"]]
    if a.status_host:
        known = [p for p in known if p.get("_status_host") == a.status_host]
    skipped = len(preds) - len(known)
    if skipped and not a.only:
        print(f"({skipped} prediction(s) skipped — not SWE-bench Pro tasks; the Verified grader owns those)")
    if not known:
        print("nothing to grade")
        return

    by_inst = collections.defaultdict(list)
    for p in known:
        by_inst[p["instance_id"]].append(p)
    jobs = []
    for iid, plist in by_inst.items():
        for p in (plist[:a.limit] if a.limit else plist):
            jobs.append((iid, p))

    app = modal.App.lookup(a.app, create_if_missing=True)
    # Some sweap-images need help booting as a Modal Sandbox:
    #  - Debian bases (e.g. NodeBB's) ship PEP 668's EXTERNALLY-MANAGED marker, which blocks
    #    Modal's own bootstrap `pip install` of its sandbox client before anything of ours runs.
    #  - Images with a custom ENTRYPOINT (e.g. NodeBB's ENTRYPOINT ["/bin/bash"]) crash Modal's
    #    process launcher on start (container exits ~immediately, exit 126) — clear it.
    images = {iid: modal.Image.from_registry(
                  tasks[iid]["image"],
                  setup_dockerfile_commands=["RUN rm -f /usr/lib/python3*/EXTERNALLY-MANAGED",
                                              "ENTRYPOINT []"])
              for iid in by_inst}

    results = json.load(open(out)) if os.path.exists(out) else {}
    done, lock = [0], threading.Lock()

    def work(job):
        iid, p = job
        t = tasks[iid]
        mnp, patch = p["model_name_or_path"], p.get("model_patch", "")
        if not patch.strip():
            res = {"instance_id": iid, "model_name_or_path": mnp, "resolved": False, "note": "empty patch"}
        else:
            try:
                resolved, summary = run_one(app, images[iid], t, patch, retries=2)
                res = {"instance_id": iid, "model_name_or_path": mnp, "resolved": resolved, **summary}
            except Exception as e:  # noqa: BLE001
                res = {"instance_id": iid, "model_name_or_path": mnp, "resolved": False, "error": str(e)[:300]}
        with lock:
            results[f"{iid}::{mnp}"] = res
            done[0] += 1
            if "error" in res:
                tag = f"ERROR {res['error']}"
            elif res.get("note"):
                tag = res["note"]
            else:
                tag = (("RESOLVED" if res["resolved"] else "unresolved")
                       + f" (f2p {res['f2p_pass']}/{res['f2p_total']}, p2p {res['p2p_pass']}/{res['p2p_total']})")
            print(f"  [{done[0]}/{len(jobs)}] {iid.split('-')[0][9:] or iid} {mnp}: {tag}")

    print(f"grading {len(jobs)} Pro prediction(s) across {len(by_inst)} instance(s), {a.workers} concurrent...")
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(work, jobs))

    json.dump(results, open(out, "w"), indent=2)
    n = sum(1 for k, r in results.items() if r.get("resolved") and r["instance_id"] in tasks)
    print(f"\nmerged into {out}: {n} Pro prediction(s) resolved")


if __name__ == "__main__":
    main()
