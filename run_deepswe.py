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

This produces RAW results only; reporting is a separate LOCAL step (benchmark_progress_report.py):
  <out>/manifest.csv                 one row per run (task,harness,model,prompt,run,status,start,end,...)
  <out>/<label>/output.log           the agent's JSON-lines log (copied from pier's agent/*.txt)
  <out>/<label>/usage.json           token totals derived from the log (opencode setups)
  <out>/<label>/reward.json          the task's grading result (reward==1 => solved)
Then, locally: `python3 benchmark_progress_report.py <out>` -> progress_report.html + CSVs + billing.json
(it pulls the real Modal bill; needs the Modal account token). `verify_report.py` checks the numbers.
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

GLM_MODEL = "zai-org/GLM-5.2-FP8"   # opencode provider model id (the tier lives in the baseURL, not here)

# setup -> how to configure pier. model/harness are REPORTING labels (benchmark_progress_report.py keys
# off the `modal*` prefix for self-hosted GPU billing and `claude` for the Anthropic token path).
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
    """The trial dir under a per-job jobs dir (identified by its verifier or agent output). Picks the
    NEWEST match, so a resumed/retried job reads its fresh trial, not a stale one left by an earlier
    interrupted attempt in the same jobdir."""
    for pat in ("*/*/verifier/reward.json", "*/*/agent/*.txt", "*/*/trial.log"):
        hits = sorted(glob.glob(os.path.join(jobdir, pat)), key=os.path.getmtime)
        if hits:
            # <jobdir>/<job>/<trial>/(verifier|agent)/...  -> the <trial> dir
            p = Path(hits[-1])
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
    # Every job keeps a small, readable <out>/<label>/ with just the essentials; the pier console
    # log goes here too (the diagnostic when a job fails).
    outdir = os.path.join(out_dir, label)
    os.makedirs(outdir, exist_ok=True)
    try:
        shutil.copy(job_log, os.path.join(outdir, "pier.log"))
    except OSError:
        pass

    if trial is None:
        tail = (Path(job_log).read_text().strip().splitlines()[-3:]
                if os.path.exists(job_log) else [])
        log(f"[FAIL ] {label} — no trial produced ({t1 - t0:.0f}s): {' / '.join(tail)}")
        status, reward = "error", None
    else:
        status, reward = read_reward(trial)
        agent_logs = sorted(glob.glob(str(trial / "agent" / "*.txt")))
        if agent_logs:
            shutil.copy(agent_logs[0], os.path.join(outdir, "output.log"))   # the report parses this
            if SETUPS[setup]["agent"] == "opencode":
                usage = usage_from_opencode_log(os.path.join(outdir, "output.log"))
                if usage:
                    with open(os.path.join(outdir, "usage.json"), "w") as f:
                        json.dump(usage, f)
        if (trial / "artifacts" / "model.patch").exists():   # small; the empty-patch diagnostic
            shutil.copy(trial / "artifacts" / "model.patch", os.path.join(outdir, "model.patch"))
        if (trial / "verifier" / "reward.json").exists():
            shutil.copy(trial / "verifier" / "reward.json", os.path.join(outdir, "reward.json"))
        log(f"[done ] {label} — {status} (reward={reward}) {t1 - t0:.0f}s")

    # Keep ONLY those copied artifacts. Delete pier's scratch: the bulky, permission-locked trial
    # tree (task-container fs, agent sessions, verifier internals) plus the temp config/console log
    # in work_dir. Runs as root in the image, so the container-owned session files are removable;
    # ignore_errors covers the raw-on-host case.
    shutil.rmtree(jobdir, ignore_errors=True)
    for f in (cfg_path, job_log):
        try:
            os.remove(f)
        except OSError:
            pass

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
                    help="name for this run's subfolder under --out [default: timestamp]. "
                         "Reuse an existing run-id to RESUME it (skips jobs already recorded pass/fail).")
    ap.add_argument("--host-ip", default=os.environ.get("HOST_IP", ""),
                    help="address Squid uses to reach the reasoning-proxy sidecar (box private IP)")
    ap.add_argument("--pier", default=os.environ.get("PIER", "pier"), help="pier executable [%(default)s]")
    ap.add_argument("--timeout-mult", type=float, default=float(os.environ.get("TIMEOUT_MULT", "1.0")),
                    help="pier agent_timeout_multiplier [%(default)s]")
    ap.add_argument("--list-tasks", action="store_true", help="print available tasks and exit")
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

    # Resume: if this run_dir already has a manifest (same --run-id, or a reused folder), skip jobs
    # already recorded as pass/fail and append to it. Errored/missing jobs are retried. This turns a
    # crash/reboot/--jobs change mid-run into "pick up where it left off" instead of redoing everything.
    manifest_path = os.path.join(run_dir, "manifest.csv")
    done_labels = set()
    if os.path.exists(manifest_path) and os.path.getsize(manifest_path) > 0:
        with open(manifest_path) as f:
            for row in csv.DictReader(f):
                if row.get("status") in ("pass", "fail") and row.get("outdir"):
                    done_labels.add(os.path.basename(row["outdir"].rstrip("/")))
        jobs = [(s, t, r) for (s, t, r) in jobs if f"{s}__{t}__run{r}" not in done_labels]
        log(f"resume: {len(done_labels)} completed jobs in {manifest_path} — skipping; {len(jobs)} left to run")
    else:
        with open(manifest_path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=MANIFEST_FIELDS).writeheader()

    log(f"DeepSWE bench: {len(setups)} setups × {len(tasks)} tasks × {args.runs} runs "
        f"= {len(jobs)} pier runs to do, {args.jobs} parallel → {run_dir}")

    def record(row):
        with _manifest_lock, open(manifest_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=MANIFEST_FIELDS).writerow(row)

    finished = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(run_job, s, t, r, args, env, run_dir, work_dir): (s, t, r)
                for (s, t, r) in jobs}
        for fut in as_completed(futs):
            s, t, r = futs[fut]
            try:
                row = fut.result()
            except Exception as e:   # one job's failure (disk, docker, etc.) must never kill the run
                log(f"[ERROR] {s}__{t}__run{r} — {type(e).__name__}: {e} (job dropped, run continues)")
                row = None
            if row:
                record(row)
            finished += 1
            log(f"  progress: {finished}/{len(jobs)}")

    log(f"done: {finished} runs -> {run_dir}")
    log("report locally:  python3 benchmark_progress_report.py " + run_dir)


if __name__ == "__main__":
    main()
