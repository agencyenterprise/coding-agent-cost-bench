#!/usr/bin/env python3
"""In-container benchmark orchestrator: generate -> grade -> report.html, one process.

This is the Docker entrypoint. Each job runs its agent INSIDE the task's prebuilt image as a sibling
container on the host daemon (Q1/Q2, via the mounted docker.sock — NOT docker-in-docker); the node+CLI
bundle is mounted read-only (Q6) and the agent edits the repo at /app. Containers are named/labelled
`bench-<runid>-*` so timeout (docker kill) and a finally + startup/shutdown label sweep leave no orphans
(Q10). A live monitor runs alongside; grading on Modal + report.html always follow. The GLM GPU stays
on Modal; this orchestrator is CPU-only.

  docker run --rm -e MODAL_ENDPOINT=... -e MODAL_KEY=... -e MODAL_SECRET=... \
      -e ANTHROPIC_API_KEY=... -e MODAL_TOKEN_ID=... -e MODAL_TOKEN_SECRET=... \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$PWD/results:/out" glm-bench --runs 1 --jobs 4

Creds come from the process environment and, if missing, from a local `.env` (via python-dotenv).
Supports plain `KEY=val` and shell `export KEY="val"`; real env / `-e` always wins. Flags: --runs,
--jobs, --tasks, --task, --models, --timeout, --rate, --list. Grading always runs.
"""
import argparse
import csv
import glob
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
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


# deep-swe (Q14): each task's sole prompt is its instruction.md, verbatim. The old prompt.v* sweep is
# gone, so the `pv` result column is fixed to one label — aggregate.py/report generation stay unchanged.
PROMPT_FILE = "instruction.md"
PROMPT_LABEL = "instr"


def is_self_hosted(model):
    return model.split("/")[0].startswith("modal")


def setup_label(harness, model):
    head = model.split("/")[0]
    return {"modal": "GLM(default)", "modal-high": "GLM(high)",
            "modal-nothink": "GLM(no-think)"}.get(head, "Opus" if harness == "claude" else head)


def short_task(name):
    t = name.replace("demo-swebench-", "")
    return t.split("__")[-1]


# ---------------------------------------------------------------- docker sibling plumbing (Q1/Q2/Q6/Q10)
DEVNULL = subprocess.DEVNULL


def pull_image(image, logpath):
    """Anonymous ECR pull (Q1/Q13). Repo is already at /app in the image, base_commit checked out.
    Retries a few times — ECR/network flakiness shouldn't sink a job. Output tees to pull.log."""
    with open(logpath, "w") as lf:
        for i in range(3):
            if subprocess.run(["docker", "pull", image], stdout=lf, stderr=subprocess.STDOUT).returncode == 0:
                return
            lf.write(f"\n[pull retry {i + 1}]\n")
            lf.flush()
            time.sleep(3)
    raise RuntimeError(f"docker pull failed after 3 tries: {image}")


def self_image():
    """Image id of the orchestrator's own container — so we can spawn a sibling from it to seed the
    bundle volume. HOSTNAME is the container's short id under a normal `docker run` (gosu preserves it)."""
    cid = os.environ.get("HOSTNAME", "")
    img = subprocess.run(["docker", "inspect", "--format", "{{.Image}}", cid],
                         capture_output=True, text=True).stdout.strip()
    if not img:
        sys.exit(f"cannot determine orchestrator image id (HOSTNAME={cid!r}); is /var/run/docker.sock mounted?")
    return img


def setup_bundle_volume(runid):
    """Q6: seed a named volume ONCE with the baked node+CLI bundle (+ opencode.jsonc) from /opt/agent,
    then mount it read-only into every task container. A named volume is the only bundle path a SIBLING
    container can see (the orchestrator's own FS isn't on the host daemon). Returns the volume name."""
    vol = f"bench-{runid}-agent"
    subprocess.run(["docker", "volume", "create", vol], stdout=DEVNULL, check=True)
    img = self_image()
    log(f">>> seeding agent bundle volume {vol} (node+CLIs, ~527M) from {img[:19]}")
    rc = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh",
                         "-v", f"{vol}:/dst", img, "-c",
                         "cp -a /opt/agent/. /dst/ && cp /app/opencode.jsonc /dst/opencode.jsonc"]).returncode
    if rc != 0:
        sys.exit("failed to seed bundle volume")
    return vol


def sweep(runid):
    """Deterministic cleanup (Q10): remove every container this run owns by label. Run at startup and
    shutdown so no orphan survives a normal run, a timeout, or a Ctrl-C."""
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=bench={runid}"],
                         capture_output=True, text=True).stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], stdout=DEVNULL, stderr=DEVNULL)


def run_one_job(job, cfg, manifest):
    """Generate one (task, harness, model, run). Returns a result dict; updates JOBS for the monitor."""
    jid = job["id"]
    task, task_abs, meta = job["task"], job["task_abs"], job["meta"]
    harness, model, run, pf = job["harness"], job["model"], job["run"], job["prompt_file"]
    pv = PROMPT_LABEL
    safe = sanitize(f"{harness}_{model}")
    outdir = os.path.join(cfg["results_dir"], f"{task}__{pv}__{safe}__run{run}")
    os.makedirs(outdir, exist_ok=True)
    logpath = os.path.join(outdir, "output.log")
    prompt = open(os.path.join(task_abs, pf)).read()

    # Q1: the task's prebuilt ECR image already has the repo at /app with base_commit checked out.
    image = meta["docker_image"]
    cname = f"bench-{cfg['runid']}-{job['idx']}"
    cpus = str(meta.get("cpus") or 2)
    mem = f"{meta.get('memory_mb') or 8192}m"
    # Q9: per-job ceiling = task.toml [agent].timeout_sec (5400); --timeout overrides for smoke tests.
    timeout = cfg["timeout"] if cfg["timeout"] is not None else (meta.get("timeout_sec") or 5400)

    # Q6: invoke the agent as the ABSOLUTE native binary from the read-only bundle mount. These are
    # bun-compiled ELFs, not node scripts — do NOT wrap in node, and do NOT touch the container PATH.
    if harness == "claude":
        agent = ["/opt/agent/bin/claude", "-p", prompt, "--model", model.split("/")[-1],
                 "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    else:
        agent = ["/opt/agent/bin/opencode", "run", prompt, "-m", model, "--format", "json", "--auto"]

    # Default-GLM + claude reach Modal/Anthropic over the container's normal egress (Q5); no proxy here.
    denv = []
    for k in ("MODAL_ENDPOINT", "MODAL_KEY", "MODAL_SECRET", "ANTHROPIC_API_KEY"):
        if os.environ.get(k):
            denv += ["-e", f"{k}={os.environ[k]}"]
    denv += ["-e", "OPENCODE_CONFIG=/opt/agent/opencode.jsonc", "-e", "NO_COLOR=1",
             "-e", "HOME=/root", "-e", "IS_SANDBOX=1"]  # IS_SANDBOX lets claude skip-perms as root

    # Q10: deterministic name + label so cleanup is a label sweep; --cpus/--memory from task.toml.
    cmd = ["docker", "run", "--name", cname, "--label", f"bench={cfg['runid']}",
           "--cpus", cpus, "--memory", mem,
           "-v", f"{cfg['bundle_vol']}:/opt/agent:ro", "-w", "/app", *denv, image, *agent]

    start = time.time()
    killed_signal = None
    with JOBS_LOCK:
        JOBS[jid] = {"setup": setup_label(harness, model), "task": short_task(task),
                     "pid": None, "start": start, "outdir": outdir, "status": "running",
                     "harness": harness, "retries": 0}
    try:
        pull_image(image, os.path.join(outdir, "pull.log"))
        attempt = 1
        while True:
            subprocess.run(["docker", "rm", "-f", cname], stdout=DEVNULL, stderr=DEVNULL)  # reuse name on retry
            with open(logpath, "w") as lf:
                proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
            with JOBS_LOCK:
                JOBS[jid]["pid"] = proc.pid
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "kill", cname], stdout=DEVNULL, stderr=DEVNULL)
                proc.wait()
                killed_signal = 9        # SIGKILL via docker kill — recorded like the old killpg path
            try:
                data = open(logpath, encoding="utf-8", errors="replace").read()
                open(logpath, "w", encoding="utf-8").write(ANSI.sub("", data))
            except OSError:
                data = ""
            if killed_signal is None and SERVER_ERR.search(data) and attempt < cfg["retries"] + 1:
                attempt += 1
                with JOBS_LOCK:
                    JOBS[jid]["retries"] = attempt - 1
                time.sleep(cfg["run_delay"])
                continue
            break
    finally:
        subprocess.run(["docker", "rm", "-f", cname], stdout=DEVNULL, stderr=DEVNULL)  # Q10: always tear down
    end = time.time()

    # Cost capture (opencode usage.json) + patch harvest land in T1b; write the empty stub the report
    # pipeline expects so nothing downstream trips over a missing file.
    if harness != "claude":
        open(os.path.join(outdir, "usage.json"), "w").write('{"sessions":[]}')

    with MANIFEST_LOCK:
        with open(manifest, "a", newline="") as f:
            csv.writer(f).writerow([task, harness, model, pv, run, outdir,
                                    f"{start:.6f}", f"{end:.6f}", f"{end - start:.2f}", "n/a"])

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

    # Rich live table only when stderr is a real terminal (docker run -it). Without a TTY (plain
    # `docker run`, CI, piped) rich disables the live display and shows nothing — so fall through to
    # the plain periodic status line instead of going silent.
    if sys.stderr.isatty():
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


# ---------------------------------------------------------------- deep-swe task discovery (Q15)
def load_task_meta(task_dir):
    """Read the deep-swe `task.toml` fields the orchestrator needs downstream (Q1/Q9/Q10/Q13). The
    agent-env limits live under `[environment]`; `timeout_sec` under `[agent]`; ids/commit under
    `[metadata]` (the parallel `[verifier.*]` values are for the separate grading step, not this)."""
    with open(os.path.join(task_dir, "task.toml"), "rb") as f:
        t = tomllib.load(f)
    meta, env, agent = t.get("metadata", {}), t.get("environment", {}), t.get("agent", {})
    ts = agent.get("timeout_sec")
    return {"task_id": meta.get("task_id") or os.path.basename(task_dir),
            "docker_image": env.get("docker_image", ""),
            "base_commit_hash": meta.get("base_commit_hash", ""),
            "timeout_sec": int(ts) if ts else None,
            "cpus": env.get("cpus"), "memory_mb": env.get("memory_mb")}


def discover_tasks(tasks_dir):
    """deep-swe Harbor tasks: every dir under `tasks/` holding a `task.toml`. The 4 dataset-level
    FILES (dataset.toml, manifest.json, manifest.schema.json, README.md) have no task.toml, so the
    glob skips them → exactly 113. Returns {task_id: task_dir}, keyed by `[metadata].task_id`."""
    out = {}
    for toml_path in sorted(glob.glob(os.path.join(tasks_dir, "*", "task.toml"))):
        meta = load_task_meta(os.path.dirname(toml_path))
        out[meta["task_id"]] = os.path.dirname(toml_path)
    return out


# ---------------------------------------------------------------- main
def build_queue(cfg):
    tasks = discover_tasks(cfg["tasks_dir"])
    if cfg["only_task"]:
        if cfg["only_task"] not in tasks:
            sys.exit(f"--task {cfg['only_task']!r} not found among {len(tasks)} deep-swe tasks")
        tasks = {cfg["only_task"]: tasks[cfg["only_task"]]}
    q = []
    for harness, model in cfg["setups"]:
        for task, task_abs in sorted(tasks.items()):
            meta = load_task_meta(task_abs)
            for run in range(1, cfg["runs"] + 1):
                q.append({"id": f"{task}|{harness}|{model}|{run}", "idx": len(q), "task": task,
                          "task_abs": task_abs, "harness": harness, "model": model,
                          "prompt_file": PROMPT_FILE, "run": run, "meta": meta})
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
    ap.add_argument("--tasks", default=os.path.join(HERE, "deep-swe", "tasks"))
    ap.add_argument("--task", default="")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--list", action="store_true",
                    help="discover tasks and exit; with --task also print its instruction.md + docker_image")
    ap.add_argument("--timeout", type=int, default=None,
                    help="per-job wall-clock ceiling in seconds; default = task.toml [agent].timeout_sec (5400)")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--rate", default="50.7")
    ap.add_argument("--results-dir", default="")
    ap.add_argument("--grade-only", default="", help="re-grade an existing results dir and exit")
    a = ap.parse_args()

    env_file = load_env()

    if a.grade_only:                       # re-grade a finished run, no generation
        modal_auth()
        log(f">>> grade-only -> {a.grade_only}")
        grade(a.grade_only, a.rate)
        log(f">>> done -> {a.grade_only}/report.html")
        return

    if a.list:                             # discover tasks and exit — no creds, no container run
        tasks = discover_tasks(a.tasks)
        if a.task:
            if a.task not in tasks:
                sys.exit(f"--task {a.task!r} not found among {len(tasks)} deep-swe tasks")
            meta = load_task_meta(tasks[a.task])
            log(f"# task_id      : {meta['task_id']}")
            log(f"# docker_image : {meta['docker_image']}")
            log(f"# base_commit  : {meta['base_commit_hash']}")
            log(f"# cpus={meta['cpus']} memory_mb={meta['memory_mb']} timeout_sec={meta['timeout_sec']}")
            log(f"# prompt ({PROMPT_FILE}, label={PROMPT_LABEL}):\n")
            sys.stdout.write(open(os.path.join(tasks[a.task], PROMPT_FILE)).read())
        else:
            for tid in sorted(tasks):
                print(tid)
            log(f">>> {len(tasks)} deep-swe tasks")
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
    runid = sanitize(os.path.basename(os.path.normpath(rdir)))   # docker --name/--label safe
    disp_timeout = a.timeout or 5400                              # monitor's ⚠ threshold only
    cfg = {"results_dir": rdir, "runid": runid, "bundle_vol": None,
           "timeout": a.timeout, "retries": a.retries, "run_delay": 2, "runs": a.runs,
           "tasks_dir": a.tasks, "only_task": a.task, "setups": setups}

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

    sweep(runid)                                    # Q10 startup sweep — clear any prior run's leftovers
    cfg["bundle_vol"] = setup_bundle_volume(runid)  # Q6 read-only node+CLI bundle, seeded once

    proxies = start_proxies([m for _, m in setups])
    try:
        if glm and not warm_endpoint(glm.split("/", 1)[1]):
            sys.exit(f"GLM endpoint {os.environ['MODAL_ENDPOINT']} not ready")

        queue = build_queue(cfg)
        log(f">>> {len(queue)} jobs across {len(setups)} setups — pool of {a.jobs}, ordered modal-first")

        from concurrent.futures import ThreadPoolExecutor
        stop = threading.Event()
        mon = threading.Thread(target=monitor_loop,
                               args=(stop, len(queue), disp_timeout, int(os.environ.get("STALL_SECS", "120")), time.time()))
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
            log(f"!! {len(killed)} job(s) hit the timeout / were killed: {killed[:5]}")
    finally:
        for p in proxies:
            p.terminate()
        sweep(runid)        # Q10 shutdown sweep — no orphan survives a normal run, timeout, or Ctrl-C
        subprocess.run(["docker", "volume", "rm", "-f", cfg["bundle_vol"]], stdout=DEVNULL, stderr=DEVNULL)

    grade(rdir, a.rate)   # always grade on Modal + bill + aggregate -> report.html
    log(f">>> done -> {host_display_path(rdir)}/report.html")


if __name__ == "__main__":
    main()
