#!/usr/bin/env python3
"""In-container benchmark orchestrator: generate -> grade -> report.html, one process.

This is the Docker entrypoint. It owns the job pool (real subprocesses in their own process groups,
so timeouts/kills take the whole tree and every exit signal is visible), a live monitor, then always
grades the SWE tasks on Modal and writes report.html. The GLM GPU stays on Modal; this is CPU-only.

  docker run --rm -e MODAL_ENDPOINT=... -e MODAL_KEY=... -e MODAL_SECRET=... \
      -e ANTHROPIC_API_KEY=... -e MODAL_TOKEN_ID=... -e MODAL_TOKEN_SECRET=... \
      -v "$PWD/out:/out" glm-bench --runs 1 --jobs 4

Creds come from the process environment and, if missing, from a local `.env` (via python-dotenv).
Supports plain `KEY=val` and shell `export KEY="val"`; real env / `-e` always wins. Flags: --runs,
--jobs, --tasks, --task, --models, --prompts, --timeout, --rate. Grading always runs.
"""
import argparse
import csv
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SERVER_ERR = re.compile(r"UnknownError|Unexpected server error", re.I)


def load_env(path=None):
    """Load `.env` into os.environ without overriding existing vars. Returns the path used, or None."""
    candidates = [path] if path else [os.path.join(os.getcwd(), ".env"), os.path.join(HERE, ".env")]
    for p in candidates:
        if p and os.path.isfile(p):
            load_dotenv(p, override=False)
            return p
    return None

DEFAULT_MODELS = [
    "opencode:modal/zai-org/GLM-5.2-FP8",
    "opencode:modal-high/zai-org/GLM-5.2-FP8",
    "opencode:modal-nothink/zai-org/GLM-5.2-FP8",
    "claude:anthropic/claude-opus-4-8",
]
# reasoning proxies: opencode can't add chat_template_kwargs, so a per-tier proxy injects them
PROXIES = {"modal-nothink": ("off", 8899, "NOTHINK_ENDPOINT"),
           "modal-high": ("high", 8898, "HIGH_ENDPOINT")}

JOBS = {}            # job_id -> live state for the monitor
JOBS_LOCK = threading.Lock()
MANIFEST_LOCK = threading.Lock()
CPU_PROCS = {}       # pid -> persistent psutil.Process (cpu_percent needs deltas between calls)
PARSE_CACHE = {}     # outdir -> (last_parse_ts, log_mtime, steps, tokens_out) — throttles log parsing


def host_display_path(path):
    """Map container /out/... to host results/... for log lines (see HOST_OUT_DIR / README mount)."""
    out = os.path.abspath(os.environ.get("OUT_DIR", "/out"))
    host = os.environ.get("HOST_OUT_DIR") or ("results" if out == "/out" else None)
    if not host:
        return path
    abs_path = os.path.abspath(path)
    if abs_path == out or abs_path.startswith(out + os.sep):
        rel = os.path.relpath(abs_path, out)
        return host if rel == "." else os.path.join(host, rel)
    return path


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def sanitize(s):
    return re.sub(r"[/ :]", "_", s)


def plabel(prompt_file):
    return prompt_file[len("prompt."):-len(".txt")] if prompt_file.startswith("prompt.") else prompt_file


def is_self_hosted(model):
    return model.split("/")[0].startswith("modal")


def setup_label(harness, model):
    head = model.split("/")[0]
    return {"modal": "GLM(default)", "modal-high": "GLM(high)",
            "modal-nothink": "GLM(no-think)"}.get(head, "Opus" if harness == "claude" else head)


def short_task(name):
    t = name.replace("demo-swebench-", "")
    return t.split("__")[-1]


# ---------------------------------------------------------------- per-job work (ported from bench.sh)
def prepare_work(task_abs, work, cache_dir):
    """Populate `work` from the task's source. Returns the external src path (or '')."""
    repo_dir = os.path.join(task_abs, "repo")
    repo_git = os.path.join(task_abs, "repo.git")
    if os.path.isdir(repo_dir):
        shutil.copytree(repo_dir, work, dirs_exist_ok=True)
        return ""
    if os.path.exists(repo_git):
        url, _, ref = open(repo_git).read().strip().partition(" ")
        ref = ref.strip()
        key = sanitize(f"{url}__{ref or 'HEAD'}")
        cache = os.path.join(cache_dir, key)
        lock = cache + ".lock"
        while True:
            try:
                os.mkdir(lock)
                break
            except FileExistsError:
                time.sleep(0.2)
        try:
            if not os.path.isdir(os.path.join(cache, ".git")):
                shutil.rmtree(cache, ignore_errors=True)
                ok = False
                for _ in range(3):
                    if subprocess.run(["git", "clone", "--quiet", url, cache]).returncode == 0:
                        ok = True
                        break
                    time.sleep(3)
                if not ok:
                    raise RuntimeError(f"git clone failed after 3 tries: {url} -> {cache}")
        finally:
            os.rmdir(lock)
        # --no-hardlinks: Docker Desktop bind-mounts (macOS host -> Linux VM) can't hardlink
        # into container /tmp; plain --local then dies with "Invalid cross-device link" (128).
        subprocess.run(["git", "-c", "advice.detachedHead=false", "clone",
                        "--local", "--no-hardlinks", "--quiet", cache, work], check=True)
        if ref:
            subprocess.run(["git", "-C", work, "-c", "advice.detachedHead=false", "checkout", "--quiet", ref],
                           check=True)
        return ""
    return ""


def snapshot_usage(state_dir, outfile):
    """opencode: copy this run's isolated DB into a temp HOME so ccusage reads only this job."""
    db = os.path.join(state_dir, "opencode", "opencode.db")
    if not os.path.exists(db):
        open(outfile, "w").write('{"sessions":[]}')
        return
    home = tempfile.mkdtemp()
    dst = os.path.join(home, ".local/share/opencode")
    os.makedirs(dst, exist_ok=True)
    for ext in ("", "-wal", "-shm"):
        try:
            shutil.copy(db + ext, dst)
        except OSError:
            pass
    env = {**os.environ, "HOME": home}
    with open(outfile, "w") as f:
        subprocess.run(["npx", "-y", "ccusage", "opencode", "session", "--json"],
                       stdout=f, stderr=subprocess.DEVNULL, env=env)
    try:
        import json
        json.load(open(outfile))
    except Exception:
        open(outfile, "w").write('{"sessions":[]}')
    shutil.rmtree(home, ignore_errors=True)


def run_one_job(job, cfg, manifest):
    """Generate one (task, harness, model, run). Returns a result dict; updates JOBS for the monitor."""
    jid = job["id"]
    task, task_abs = job["task"], job["task_abs"]
    harness, model, run, pf = job["harness"], job["model"], job["run"], job["prompt_file"]
    pv = plabel(pf)
    safe = sanitize(f"{harness}_{model}")
    outdir = os.path.join(cfg["results_dir"], f"{task}__{pv}__{safe}__run{run}")
    os.makedirs(outdir, exist_ok=True)
    logpath = os.path.join(outdir, "output.log")
    prompt = open(os.path.join(task_abs, pf)).read()

    work = tempfile.mkdtemp()
    state_dir = tempfile.mkdtemp()
    env = {**os.environ, "XDG_DATA_HOME": state_dir, "XDG_STATE_HOME": state_dir,
           "XDG_CONFIG_HOME": state_dir, "OPENCODE_CONFIG": os.path.join(HERE, "opencode.jsonc"),
           "NO_COLOR": "1"}
    prepare_work(task_abs, work, cfg["cache_dir"])

    if os.path.exists(os.path.join(task_abs, "setup.sh")):
        with open(os.path.join(outdir, "setup.log"), "w") as sl:
            subprocess.run(["bash", os.path.join(task_abs, "setup.sh")], cwd=work,
                           env={**env, "TASK_REPO_SRC": ""}, stdout=sl, stderr=subprocess.STDOUT)

    if subprocess.run(["git", "-C", work, "rev-parse", "--is-inside-work-tree"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        ga = ["git", "-C", work, "-c", "user.email=bench@local", "-c", "user.name=bench"]
        subprocess.run(ga + ["add", "-A"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(ga + ["commit", "-qm", "post-setup baseline"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    agent_path = env["PATH"]
    venv_bin = os.path.join(work, ".venv/bin")
    if os.path.exists(os.path.join(venv_bin, "python")):
        agent_path = venv_bin + ":" + agent_path

    if harness == "claude":
        cmd = ["claude", "-p", prompt, "--model", model.split("/")[-1],
               "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    else:
        cmd = ["opencode", "run", prompt, "-m", model, "--format", "json", "--auto"]

    start = time.time()
    killed_signal = None
    with JOBS_LOCK:
        JOBS[jid] = {"setup": setup_label(harness, model), "task": short_task(task),
                     "pid": None, "start": start, "outdir": outdir, "status": "running",
                     "harness": harness, "retries": 0}
    attempt = 1
    while True:
        with open(logpath, "w") as lf:
            proc = subprocess.Popen(cmd, cwd=work, env={**env, "PATH": agent_path},
                                    stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        with JOBS_LOCK:
            JOBS[jid]["pid"] = proc.pid
        try:
            proc.wait(timeout=cfg["timeout"])
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        rc = proc.returncode
        if rc is not None and rc < 0:
            killed_signal = -rc          # killed by a signal (e.g. 9 = SIGKILL/OOM) — now visible
        try:
            data = open(logpath, encoding="utf-8", errors="replace").read()
            open(logpath, "w", encoding="utf-8").write(ANSI.sub("", data))
        except OSError:
            data = ""
        if SERVER_ERR.search(data) and attempt < cfg["retries"] + 1:
            attempt += 1
            with JOBS_LOCK:
                JOBS[jid]["retries"] = attempt - 1
            time.sleep(cfg["run_delay"])
            continue
        break
    end = time.time()

    if harness != "claude":
        snapshot_usage(state_dir, os.path.join(outdir, "usage.json"))

    # harvest the agent's change (git repos: just the diff; else the whole tree)
    if subprocess.run(["git", "-C", work, "rev-parse", "--is-inside-work-tree"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        with open(os.path.join(outdir, "model.patch"), "w") as pf_out:
            subprocess.run(["git", "-C", work, "-c", "core.fileMode=false", "diff", "HEAD"],
                           stdout=pf_out, stderr=subprocess.DEVNULL)
    else:
        fr = os.path.join(outdir, "final_repo")
        shutil.copytree(work, fr, dirs_exist_ok=True)

    with MANIFEST_LOCK:
        with open(manifest, "a", newline="") as f:
            csv.writer(f).writerow([task, harness, model, pv, run, outdir,
                                    f"{start:.6f}", f"{end:.6f}", f"{end - start:.2f}", "n/a"])
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(state_dir, ignore_errors=True)

    status = "done" if killed_signal is None else f"KILLED sig{killed_signal}"
    with JOBS_LOCK:
        JOBS[jid]["status"] = status
        JOBS[jid]["end"] = end
    return {"id": jid, "killed": killed_signal, "dur": end - start}


# ---------------------------------------------------------------- reasoning proxies + warm
def start_proxies(models):
    procs = []
    started = set()
    for m in models:
        head = m.split("/")[0]
        if head in PROXIES and head not in started:
            mode, port, envvar = PROXIES[head]
            log(f"starting reasoning proxy [{mode}] on :{port}")
            lf = open(os.path.join(os.environ["OUT_DIR_RUN"], f"proxy_{mode}.log"), "w")
            p = subprocess.Popen([sys.executable, os.path.join(HERE, "reasoning_proxy.py"),
                                  "--reasoning", mode, "--port", str(port)], stdout=lf, stderr=subprocess.STDOUT)
            procs.append(p)
            os.environ[envvar] = f"http://127.0.0.1:{port}/v1"
            started.add(head)
            time.sleep(1)
    return procs


def endpoint_up():
    """Quick liveness probe: does the endpoint answer /models with 200? (no generation, short timeout)"""
    ep = os.environ.get("MODAL_ENDPOINT")
    req = urllib.request.Request(ep + "/models", headers={
        "Modal-Key": os.environ.get("MODAL_KEY", ""), "Modal-Secret": os.environ.get("MODAL_SECRET", "")})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_endpoint():
    """Idempotent provision: create/reuse the Modal endpoint and wait until it's live. Aborts on failure."""
    rc = subprocess.run(["bash", os.path.join(HERE, "setup_auto_endpoint.sh")]).returncode
    if rc != 0:
        sys.exit(f"endpoint provisioning failed (exit {rc})")


def warm_endpoint(model_id):
    ep = os.environ.get("MODAL_ENDPOINT")
    log("warming GLM endpoint (cold 8xB200 can take a few minutes)...")
    body = ('{"model":"%s","messages":[{"role":"user","content":"ok"}],"max_tokens":1}' % model_id).encode()
    for i in range(int(os.environ.get("WARM_TRIES", "16"))):
        req = urllib.request.Request(ep + "/chat/completions", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Modal-Key": os.environ.get("MODAL_KEY", ""),
                                              "Modal-Secret": os.environ.get("MODAL_SECRET", "")})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                if r.status == 200:
                    log("  endpoint ready (HTTP 200)")
                    return True
        except Exception as e:
            code = getattr(e, "code", "000")
            log(f"  [{i + 1}] HTTP {code} — waiting {os.environ.get('WARM_SLEEP', '15')}s...")
        time.sleep(int(os.environ.get("WARM_SLEEP", "15")))
    return False


# ---------------------------------------------------------------- monitor
def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def tree_rss(pid, ps):
    """Resident memory of the job = the agent process + all its children (node workers, pip, pytest)."""
    if not ps or not pid:
        return 0
    try:
        p = ps.Process(pid)
        rss = p.memory_info().rss
        for c in p.children(recursive=True):
            try:
                rss += c.memory_info().rss
            except Exception:
                pass
        return rss
    except Exception:
        return 0


def mem_limit():
    """The memory ceiling that matters for OOM: the container's cgroup limit, else host total."""
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(p).read().strip()
            if v.isdigit() and int(v) < (1 << 62):   # cgroup 'max'/unlimited is a huge sentinel
                return int(v)
        except Exception:
            pass
    ps = _psutil()
    return ps.virtual_memory().total if ps else 0


def tree_cpu(pid, ps):
    """CPU% of the agent process (persistent Process handle so cpu_percent measures the last interval).
    Can exceed 100% on multiple cores. Mostly ~0 while waiting on the model — that's the useful bit."""
    if not ps or not pid:
        return 0.0
    try:
        p = CPU_PROCS.get(pid)
        if p is None or p.pid != pid:
            p = ps.Process(pid)
            CPU_PROCS[pid] = p
            p.cpu_percent(None)          # prime; first reading is meaningless
        return p.cpu_percent(None)
    except Exception:
        CPU_PROCS.pop(pid, None)
        return 0.0


def parse_progress(outdir, harness):
    """Steps (agent turns) and output tokens so far, reusing aggregate.py's log parsers so the live
    numbers match the report. Throttled: reparse only when the log changed and >=5s since last parse."""
    log_path = os.path.join(outdir, "output.log")
    try:
        mt = os.stat(log_path).st_mtime
    except OSError:
        return 0, 0
    now = time.time()
    ent = PARSE_CACHE.get(outdir)
    if ent and now - ent[0] < 5 and ent[1] == mt:
        return ent[2], ent[3]
    try:
        from aggregate import log_stats, claude_stats
        s = claude_stats(outdir) if harness == "claude" else log_stats(outdir)
        steps, tok = int(s.get("steps") or 0), int(s.get("out") or 0)
    except Exception:
        steps, tok = 0, 0
    PARSE_CACHE[outdir] = (now, mt, steps, tok)
    return steps, tok


def _kt(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def render_rows(now, timeout, stall):
    ps = _psutil()
    with JOBS_LOCK:
        items = list(JOBS.values())
    running = [j for j in items if j["status"] == "running"]
    done = [j for j in items if j["status"] != "running"]
    rows = []
    total_mem = 0
    for j in sorted(running, key=lambda j: j["start"]):
        el = int(now - j["start"])
        try:
            quiet = int(now - os.stat(os.path.join(j["outdir"], "output.log")).st_mtime)
        except OSError:
            quiet = el
        mem = tree_rss(j["pid"], ps)
        total_mem += mem
        cpu = tree_cpu(j["pid"], ps)
        steps, tok = parse_progress(j["outdir"], j.get("harness", ""))
        retries = j.get("retries", 0)
        flag = (f"↺{retries} " if retries else "") + ("⚠" if (quiet > stall or el > timeout * 0.8) else "")
        rows.append((j["setup"], j["task"], str(j["pid"] or "-"), _hms(el),
                     _sz(mem) if mem else "-", f"{cpu:.0f}%", str(steps), _kt(tok),
                     _hms(quiet), flag.strip()))
    return rows, len(done), len(running), total_mem


def _hms(s):
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _sz(b):
    return f"{b // 1048576}M" if b >= 1048576 else (f"{b // 1024}K" if b >= 1024 else f"{b}B")


def monitor_loop(stop, total, timeout, stall, run_start):
    """Live table via rich if available; otherwise a throttled plain status line."""
    limit = mem_limit()

    def header(ndone, nrun, tmem):
        s = f"bench · {ndone}/{total} done · {nrun} running · {_hms(int(time.time() - run_start))}"
        if tmem:
            s += f" · mem {_sz(tmem)}" + (f"/{_sz(limit)}" if limit else "")
        return s

    try:
        from rich.console import Console
        from rich.live import Live
        from rich.table import Table
        console = Console(stderr=True)

        def make():
            rows, ndone, nrun, tmem = render_rows(time.time(), timeout, stall)
            near = limit and tmem > limit * 0.85               # near the OOM ceiling -> red title
            t = Table(title=header(ndone, nrun, tmem),
                      title_style="bold red" if near else "bold", expand=False)
            for c in ("setup", "task", "pid", "elapsed", "mem", "cpu", "steps", "tok", "quiet", ""):
                t.add_column(c, justify="left" if c in ("setup", "task") else "right")
            for r in rows:
                t.add_row(*r, style="yellow" if r[9] else None)
            return t

        with Live(make(), console=console, refresh_per_second=2, transient=False) as live:
            while not stop.is_set():
                live.update(make())
                time.sleep(1)
            live.update(make())
        return
    except ImportError:
        pass
    last = ""
    while not stop.is_set():
        _rows, ndone, nrun, tmem = render_rows(time.time(), timeout, stall)
        line = header(ndone, nrun, tmem)
        if line != last:
            log(line)
            last = line
        time.sleep(5)


# ---------------------------------------------------------------- main
def build_queue(cfg):
    q = []
    prompts = cfg["prompts"]
    tasks = sorted(d for d in os.listdir(cfg["tasks_dir"])
                   if os.path.exists(os.path.join(cfg["tasks_dir"], d, "prompt.v1.txt")))
    if cfg["only_task"]:
        tasks = [t for t in tasks if t == cfg["only_task"]]
    for harness, model in cfg["setups"]:
        for task in tasks:
            task_abs = os.path.join(cfg["tasks_dir"], task)
            for pf in prompts:
                if not os.path.exists(os.path.join(task_abs, pf)):
                    continue
                for run in range(1, cfg["runs"] + 1):
                    q.append({"id": f"{task}|{harness}|{model}|{pf}|{run}", "task": task,
                              "task_abs": task_abs, "harness": harness, "model": model,
                              "prompt_file": pf, "run": run})
    return q


def modal_auth():
    """Authenticate the Modal CLI for grading sandboxes, from env only (no-op if a token isn't set)."""
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        subprocess.run(["modal", "token", "set", "--token-id", os.environ["MODAL_TOKEN_ID"],
                        "--token-secret", os.environ["MODAL_TOKEN_SECRET"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def grade(rdir, rate):
    """Grade + report, all Python (was grade_swe.sh): harvest patches -> run each in its Modal
    sandbox -> pull the run-window bill -> aggregate with the official pass/fail into report.html."""
    py = sys.executable

    def run(script, *args):
        return subprocess.run([py, os.path.join(HERE, script), *args])

    log(">>> [1/4] harvest agent patches -> predictions.jsonl")
    run("make_predictions.py", "--results-dir", rdir)
    log(">>> [2/4] grade on Modal (per-instance sandboxes)")
    run("swe_eval_modal.py", "--predictions", os.path.join(rdir, "predictions.jsonl"))
    log(">>> [3/4] actual endpoint bill for the run window -> billing.json")
    if run("billing.py", "--results-dir", rdir).returncode != 0:
        log("    (billing pull failed — report will skip the ground-truth line)")
    log(">>> [4/4] aggregate with the official grades -> report.html")
    run("aggregate.py", "--results-dir", rdir, "--rate", rate, "--no-open")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--tasks", default=os.path.join(HERE, "tasks"))
    ap.add_argument("--task", default="")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--prompts", default="prompt.v2.txt")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--rate", default="50.7")
    ap.add_argument("--results-dir", default="")
    ap.add_argument("--cache-dir", default=os.environ.get("CACHE_DIR", os.path.join(HERE, ".cache/repos")),
                    help="where cloned task repos are cached (mount a host dir to reuse across runs)")
    ap.add_argument("--grade-only", default="", help="re-grade an existing results dir and exit")
    a = ap.parse_args()

    env_file = load_env()

    if a.grade_only:                       # re-grade a finished run, no generation
        modal_auth()
        log(f">>> grade-only -> {a.grade_only}")
        grade(a.grade_only, a.rate)
        log(f">>> done -> {a.grade_only}/report.html")
        return

    for v in ("MODAL_ENDPOINT", "MODAL_KEY", "MODAL_SECRET"):
        if not os.environ.get(v):
            hint = f"pass -e {v}=... or put {v}=... in .env"
            if env_file:
                hint += f" (loaded {env_file}, key missing)"
            sys.exit(f"missing env {v} ({hint})")

    setups = []
    for e in re.split(r"[,\s]+", a.models.strip()):
        if e.startswith("claude:"):
            setups.append(("claude", e[len("claude:"):]))
        elif e.startswith("opencode:"):
            setups.append(("opencode", e[len("opencode:"):]))
        else:
            setups.append(("opencode", e))

    out = os.environ.get("OUT_DIR", "/out")
    rdir = a.results_dir or os.path.join(out, "aep-" + time.strftime("%Y-%m-%dT%H%M%S"))
    os.makedirs(rdir, exist_ok=True)
    os.environ["OUT_DIR_RUN"] = rdir
    cfg = {"results_dir": rdir, "cache_dir": a.cache_dir,
           "timeout": a.timeout, "retries": a.retries, "run_delay": 2, "runs": a.runs,
           "prompts": re.split(r"[,\s]+", a.prompts.strip()), "tasks_dir": a.tasks,
           "only_task": a.task, "setups": setups}
    os.makedirs(cfg["cache_dir"], exist_ok=True)

    manifest = os.path.join(rdir, "manifest.csv")
    with open(manifest, "w", newline="") as f:
        csv.writer(f).writerow(["task", "harness", "model", "prompt", "run", "outdir",
                                "start", "end", "duration_s", "status"])

    modal_auth()   # Modal CLI auth for the grading sandboxes + endpoint provisioning (env only)

    # Always check the endpoint; provision it (idempotent) only if it isn't up.
    glm = next((m for h, m in setups if h == "opencode" and is_self_hosted(m)), None)
    if glm:
        if endpoint_up():
            log(">>> GLM endpoint is up")
        else:
            log(">>> GLM endpoint not reachable — provisioning (create-if-missing, wait until live)")
            ensure_endpoint()

    proxies = start_proxies([m for _, m in setups])
    try:
        if glm and not warm_endpoint(glm.split("/", 1)[1]):
            sys.exit(f"GLM endpoint {os.environ['MODAL_ENDPOINT']} not ready")

        queue = build_queue(cfg)
        log(f">>> {len(queue)} jobs across {len(setups)} setups — pool of {a.jobs}, ordered modal-first")

        from concurrent.futures import ThreadPoolExecutor
        stop = threading.Event()
        mon = threading.Thread(target=monitor_loop,
                               args=(stop, len(queue), a.timeout, int(os.environ.get("STALL_SECS", "120")), time.time()))
        mon.start()
        killed = []
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = [ex.submit(run_one_job, j, cfg, manifest) for j in queue]
            for fu in futs:
                r = fu.result()
                if r["killed"]:
                    killed.append(r["id"])
        stop.set()
        mon.join()
        if killed:
            log(f"!! {len(killed)} job(s) killed by a signal (likely OOM — lower --jobs): {killed[:5]}")
    finally:
        for p in proxies:
            p.terminate()

    grade(rdir, a.rate)   # always grade on Modal + bill + aggregate -> report.html
    log(f">>> done -> {host_display_path(rdir)}/report.html")


if __name__ == "__main__":
    main()
