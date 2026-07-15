#!/usr/bin/env python3
"""DeepSWE cost benchmark orchestrator: run each setup on each task N times through pier, then report.

Each (setup × task × run) is one `pier run` on a contamination-free DeepSWE task (Harbor format,
native `--env docker`). pier injects the agent CLI into the task's own image, runs a Squid egress
proxy, and grades with the task's tests -> verifier/reward.json. We drive four setups:

    glm-default   opencode -> GLM endpoint directly       (max thinking)
    glm-high      opencode -> reasoning-proxy /high/v1     (reasoning_effort:high)
    glm-nothink   opencode -> reasoning-proxy /nothink/v1  (enable_thinking:false)
    opus          claude-code -> Anthropic API            (Claude Opus 4.8)

The GLM tiers share ONE reasoning-proxy sidecar on http://<HOST_IP>:80 (pier's Squid only allows
ports 80/443, so tiers are distinguished by URL path, not port). --host-ip is the address Squid uses
to reach the sidecar (the box's private IP; the entrypoint fills it from $HOST_IP).

Output mirrors the shape aggregate.py/billing.py already expect, so they run unchanged:
  <out>/manifest.csv                 one row per run (task,harness,model,prompt,run,status,start,end,...)
  <out>/<label>/output.log           the agent's JSON-lines log (copied from pier's agent/*.txt)
  <out>/<label>/usage.json           token totals derived from the log (opencode setups)
Then: billing.py (real Modal AEP bill over the window) -> aggregate.py -> report.html + per_run.csv
+ summary.csv.
"""
import argparse
import csv
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

GLM_MODEL = "zai-org/GLM-5.2-FP8"   # opencode provider model id (the tier lives in the baseURL, not here)

# setup -> how to configure pier. model/harness are REPORTING labels (aggregate.py keys off the
# `modal*` prefix for self-hosted GPU billing and `claude` for the Anthropic token path).
SETUPS = {
    "glm-default": {"agent": "opencode", "harness": "opencode", "tier": None,
                    "model": f"modal/{GLM_MODEL}"},
    "glm-high":    {"agent": "opencode", "harness": "opencode", "tier": "high",
                    "model": f"modal-high/{GLM_MODEL}"},
    "glm-nothink": {"agent": "opencode", "harness": "opencode", "tier": "nothink",
                    "model": f"modal-nothink/{GLM_MODEL}"},
    "opus":        {"agent": "claude-code", "harness": "claude", "tier": None,
                    "model": "claude-code/claude-opus-4-8"},
}

MANIFEST_FIELDS = ["task", "harness", "model", "prompt", "run", "status",
                   "start", "end", "duration_s", "outdir"]

_print_lock = threading.Lock()
_manifest_lock = threading.Lock()


def log(msg):
    with _print_lock:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


def base_url(setup, env, host_ip):
    """opencode baseURL for a GLM setup. default hits the endpoint directly (allowlist = the Modal
    host); high/nothink hit the sidecar by path prefix (allowlist = HOST_IP)."""
    tier = SETUPS[setup]["tier"]
    if tier is None:
        return env["MODAL_ENDPOINT"]
    return f"http://{host_ip}/{tier}/v1"


def build_config(setup, task, tasks_dir, env, host_ip, timeout_mult):
    """A pier JobConfig (JSON; pier accepts yaml or json) for one setup+task, one trial."""
    s = SETUPS[setup]
    cfg = {
        "environment": {"type": "docker"},
        "agent_timeout_multiplier": timeout_mult,
        "n_concurrent_trials": 1,
        "datasets": [{"path": tasks_dir, "task_names": [task]}],
    }
    if s["agent"] == "opencode":
        cfg["agents"] = [{
            "name": "opencode",
            "model_name": f"modal/{GLM_MODEL}",
            "kwargs": {"opencode_config": {"provider": {"modal": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": base_url(setup, env, host_ip),
                    "apiKey": "dummy",
                    "headers": {"Modal-Key": env["MODAL_KEY"], "Modal-Secret": env["MODAL_SECRET"]},
                },
                "models": {GLM_MODEL: {}},
            }}}},
        }]
    else:  # claude-code (Opus)
        cfg["agents"] = [{
            "name": "claude-code",
            "model_name": "claude-opus-4-8",
            "env": {"ANTHROPIC_API_KEY": env.get("ANTHROPIC_API_KEY", "")},
        }]
    return cfg


def find_trial(jobdir):
    """The single trial dir under a per-job jobs dir (identified by its verifier or agent output)."""
    for pat in ("*/*/verifier/reward.json", "*/*/agent/*.txt", "*/*/trial.log"):
        hits = glob.glob(os.path.join(jobdir, pat))
        if hits:
            # <jobdir>/<job>/<trial>/(verifier|agent)/...  -> the <trial> dir
            p = Path(hits[0])
            return p.parent.parent
    return None


def read_reward(trial):
    """(status, reward) from verifier/reward.json. pass = fully solved (reward >= 1.0)."""
    rj = trial / "verifier" / "reward.json"
    if not rj.exists():
        return "error", None
    try:
        d = json.loads(rj.read_text())
    except Exception:
        return "error", None
    reward = d.get("reward")
    if reward is None:
        return "error", None
    return ("pass" if float(reward) >= 1.0 else "fail"), reward


def usage_from_opencode_log(logpath):
    """ccusage-shaped token totals from an opencode JSON-lines log, so aggregate.load_usage() reports
    real GLM token counts (it reads usage.json, not the log, for tokens_in/out)."""
    fresh = cwrite = cread = out = 0
    try:
        for line in open(logpath):
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("type") != "step_finish":
                continue
            tk = (ev.get("part", {}) or {}).get("tokens", {}) or {}
            fresh += int(tk.get("input", 0) or 0)
            out += int(tk.get("output", 0) or 0)
            cache = tk.get("cache", {}) or {}
            cwrite += int(cache.get("write", 0) or 0)
            cread += int(cache.get("read", 0) or 0)
    except Exception:
        return None
    return {"sessions": [{"inputTokens": fresh, "cacheCreationTokens": cwrite,
                          "cacheReadTokens": cread, "outputTokens": out, "totalCost": 0}]}


def run_job(setup, task, run, args, env, out_dir, work_dir):
    """One `pier run`; returns the manifest row (or None on a hard launch failure)."""
    label = f"{setup}__{task}__run{run}"
    jobdir = os.path.join(work_dir, label)
    cfg = build_config(setup, task, args.tasks_dir, env, args.host_ip, args.timeout_mult)
    cfg_path = os.path.join(work_dir, f"{label}.json")
    os.makedirs(work_dir, exist_ok=True)
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)

    cmd = [args.pier, "run", "-c", cfg_path, "-y", "-o", jobdir]
    job_log = os.path.join(work_dir, f"{label}.log")   # tail -f this to watch a job live
    log(f"[start] {label}  (tail: {job_log})")
    t0 = time.time()
    with open(job_log, "w") as lf:
        subprocess.run(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT, text=True)
    t1 = time.time()

    trial = find_trial(jobdir)
    if trial is None:
        try:
            tail = Path(job_log).read_text().strip().splitlines()[-3:]
        except Exception:
            tail = []
        log(f"[FAIL ] {label} — no trial produced ({t1 - t0:.0f}s): {' / '.join(tail)}")
        status, reward = "error", None
        outdir = ""
    else:
        status, reward = read_reward(trial)
        # copy the agent log to <out>/<label>/output.log so aggregate.py can parse it
        outdir = os.path.join(out_dir, label)
        os.makedirs(outdir, exist_ok=True)
        agent_logs = sorted(glob.glob(str(trial / "agent" / "*.txt")))
        if agent_logs:
            shutil.copy(agent_logs[0], os.path.join(outdir, "output.log"))
            if SETUPS[setup]["agent"] == "opencode":
                usage = usage_from_opencode_log(os.path.join(outdir, "output.log"))
                if usage:
                    with open(os.path.join(outdir, "usage.json"), "w") as f:
                        json.dump(usage, f)
        # keep the reward + config next to the log for auditing
        if (trial / "verifier" / "reward.json").exists():
            shutil.copy(trial / "verifier" / "reward.json", os.path.join(outdir, "reward.json"))
        log(f"[done ] {label} — {status} (reward={reward}) {t1 - t0:.0f}s")

    return {
        "task": task, "harness": SETUPS[setup]["harness"], "model": SETUPS[setup]["model"],
        "prompt": "v1", "run": run, "status": status,
        "start": f"{t0:.3f}", "end": f"{t1:.3f}", "duration_s": f"{t1 - t0:.1f}",
        "outdir": outdir,
    }


def discover_tasks(tasks_dir):
    return sorted(p.name for p in Path(tasks_dir).iterdir()
                  if p.is_dir() and (p / "task.toml").exists())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setups", default="glm-default,glm-high,glm-nothink,opus",
                    help="comma list of %s [%%(default)s]" % list(SETUPS))
    ap.add_argument("--tasks", default="all", help="comma list of task names, or 'all' [%(default)s]")
    ap.add_argument("--runs", type=int, default=1, help="attempts per (setup, task) [%(default)s]")
    ap.add_argument("--jobs", type=int, default=4, help="parallel pier runs [%(default)s]")
    ap.add_argument("--tasks-dir", default=os.environ.get("TASKS_DIR", "deep-swe-main/tasks"))
    ap.add_argument("--out", default=os.environ.get("OUT_DIR", "results"),
                    help="results dir (report + csvs land here) [%(default)s]")
    ap.add_argument("--work-dir", default=os.environ.get("WORK_DIR", ""),
                    help="pier job tree dir [default: <out>/<run-id>/pier-jobs]")
    ap.add_argument("--run-id", default=os.environ.get("RUN_ID", ""),
                    help="name for this run's subfolder under --out [default: timestamp]")
    ap.add_argument("--host-ip", default=os.environ.get("HOST_IP", ""),
                    help="address Squid uses to reach the reasoning-proxy sidecar (box private IP)")
    ap.add_argument("--pier", default=os.environ.get("PIER", "pier"), help="pier executable [%(default)s]")
    ap.add_argument("--timeout-mult", type=float, default=float(os.environ.get("TIMEOUT_MULT", "1.0")),
                    help="pier agent_timeout_multiplier [%(default)s]")
    ap.add_argument("--list-tasks", action="store_true", help="print available tasks and exit")
    ap.add_argument("--no-billing", action="store_true", help="skip the Modal AEP billing pull")
    ap.add_argument("--no-aggregate", action="store_true", help="skip report generation")
    args = ap.parse_args()

    if args.list_tasks:
        for t in discover_tasks(args.tasks_dir):
            print(t)
        return

    setups = [s.strip() for s in args.setups.split(",") if s.strip()]
    for s in setups:
        if s not in SETUPS:
            sys.exit(f"unknown setup {s!r}; choices: {list(SETUPS)}")
    tasks = discover_tasks(args.tasks_dir) if args.tasks == "all" \
        else [t.strip() for t in args.tasks.split(",") if t.strip()]
    if not tasks:
        sys.exit(f"no tasks found under {args.tasks_dir}")

    env = dict(os.environ)
    need_glm = any(s.startswith("glm") for s in setups)
    need_sidecar = any(SETUPS[s]["tier"] for s in setups)
    for var in (["MODAL_ENDPOINT", "MODAL_KEY", "MODAL_SECRET"] if need_glm else []):
        if not env.get(var):
            sys.exit(f"{var} is required for GLM setups (set it in the environment / .env)")
    if need_sidecar and not args.host_ip:
        sys.exit("high/nothink setups need --host-ip (the box IP Squid uses to reach the sidecar)")
    if "opus" in setups and not env.get("ANTHROPIC_API_KEY"):
        sys.exit("opus needs ANTHROPIC_API_KEY")

    # each invocation gets its own timestamped folder under --out, so repeated runs never clobber.
    # the pier job tree lives inside it too — so a single host-aligned mount of --out holds everything
    # (report, csvs, per-run logs, and the docker-out-of-docker job tree). See entrypoint.sh.
    run_id = args.run_id or datetime.now().strftime("%Y-%m-%d__%H-%M-%S")
    run_dir = os.path.join(args.out, run_id)
    work_dir = args.work_dir or os.path.join(run_dir, "pier-jobs")
    os.makedirs(run_dir, exist_ok=True)
    # Interleave setups (task-outer, setup-inner) so consecutive jobs cycle through the setups
    # rather than running all of one setup first. This (a) mixes endpoint load — you get ~a few of
    # each tier in flight instead of N concurrent max-reasoning streams saturating the GPU — and
    # (b) surfaces a full cross-setup comparison on the first tasks early, instead of hours in.
    jobs = [(s, t, r) for t in tasks for r in range(1, args.runs + 1) for s in setups]
    log(f"DeepSWE bench: {len(setups)} setups × {len(tasks)} tasks × {args.runs} runs "
        f"= {len(jobs)} pier runs, {args.jobs} parallel → {run_dir}")

    manifest_path = os.path.join(run_dir, "manifest.csv")
    with open(manifest_path, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=MANIFEST_FIELDS).writeheader()

    def record(row):
        with _manifest_lock, open(manifest_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=MANIFEST_FIELDS).writerow(row)

    done = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(run_job, s, t, r, args, env, run_dir, work_dir): (s, t, r)
                for (s, t, r) in jobs}
        for fut in as_completed(futs):
            row = fut.result()
            if row:
                record(row)
            done += 1
            log(f"  progress: {done}/{len(jobs)}")

    if need_glm and not args.no_billing:
        try:
            log("billing: pulling the Modal AEP bill for the run window…")
            subprocess.run([sys.executable, str(HERE / "billing.py"), "--results-dir", run_dir],
                           env=env, check=False)
        except Exception as e:
            log(f"billing skipped: {e}")

    if not args.no_aggregate:
        subprocess.run([sys.executable, str(HERE / "aggregate.py"), "--results-dir", run_dir, "--no-open"],
                       env=env, check=False)


if __name__ == "__main__":
    main()
