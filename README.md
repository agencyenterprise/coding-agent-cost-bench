# Coding-agent cost benchmark

Runs the **same tasks** through the **same harness** ([opencode](https://opencode.ai)) across
models (self-hosted **GLM-5.2 on Modal** vs **Claude**), then reports **success rate** and
**cost per successful task** — the honest metric, not raw $/token. Built for the Modal case
study / DARPA DICE proposal.

Same harness for every model isolates the **model** as the only variable (comparing the Claude
Code *product* vs GLM-in-opencode would mix model + harness).

**Three steps:** [`./setup.sh`](#1-setup) → [`./run_bench.sh`](#2-run) → [`python judge.py`](#3-judge).

---

## 1. Setup

**Prereqs:** `opencode`, `python3`, and on macOS `brew install coreutils` (for `gtimeout`).

**Keys** — copy the template and fill it in:
```bash
cp .env.example .env      # MODAL_ENDPOINT, MODAL_KEY, MODAL_SECRET, ANTHROPIC_API_KEY, GEMINI_API_KEY, ...
```
`run_bench.sh` and `judge.py` `source .env` themselves — no need to export by hand. Providers
live in [opencode.jsonc](opencode.jsonc) (committed, secrets via `{env:...}`).

**GLM endpoint** — bring up the Modal auto-endpoint (idempotent, reuses the weights volume,
enforces one proxy token, waits out provisioning):
```bash
pip install modal && modal setup     # once, to auth the CLI
./setup.sh                           # creates/echoes the 8×B200 GLM-5.2 endpoint
```
Re-run `./setup.sh` anytime; it only creates what's missing. `./setup.sh --help` for flags.

## 2. Run
```bash
./run_bench.sh                 # GLM + Claude, all tasks, 30 parallel jobs, auto-aggregates
```
Defaults: `--runs 1`, both models, `--jobs 30`. Common flags:

| Flag | Meaning | Default |
|---|---|---|
| `-r, --runs N` | repeats per (task, model) | 1 |
| `-m, --models "a b"` | model refs (space/comma) | GLM + Claude Opus |
| `-j, --jobs N` | parallel jobs — **the cost lever for GLM** | 30 |
| `-t, --tasks DIR` | tasks directory | `./tasks` |
| `--timeout SECS` | kill a stuck agent | 500 |
| `--delete-repo` | discard the mutated repo | keep |

Writes `results/manifest.csv` + per-run logs, then runs `aggregate.py` →
`results/summary.csv` + `results_detailed.csv`.

**Two harnesses.** Every model runs in **opencode** (isolates the *model* — the clean comparison).
Anthropic models *additionally* run in **Claude Code's own CLI** (`claude -p`), the real-world
product comp — since that's how people actually use Opus. GLM/GPT/Gemini can't run in Claude Code
(Anthropic-only), so they're opencode-only. Claude Code reports its own cost/usage/turns, so those
rows carry `cost_basis = claude_code`. Requires the `claude` CLI on PATH.

## 3. Judge
Turn the raw runs into the final report — numbers + a blinded LLM review of each transcript+diff:
```bash
python judge.py --judge gemini      # gemini | openai | anthropic | glm  (pick one NOT in the comparison)
```
Writes **`results/report.md`**: the numbers table, a **timeline** (start/end + overlap per model),
a **cost breakdown**, a **break-even table** (how many parallel tasks on Modal beat Claude), and
short, blinded per-task notes. All sections are generated from `summary.csv`, so re-running is safe.

---

## Cost model (the honest bit)
Every cost carries a `cost_basis`:
- **Claude / API models** → `cost = tokens × price` (incl. prompt caching), from ccusage.
  `cost_basis = api_ccusage`. You pay per token, $0 when idle.
- **GLM on Modal** → you rent the whole 8×B200 endpoint (~$50.7/hr while up). We charge **only the
  minutes the model actually ran** — the *union* of run intervals (parallel runs count once, not
  summed) × the hourly rate — excluding idle warm/scale-down. `cost_basis = gpu_active`. Override
  the rate with `GLM_GPU_HOURLY_USD`.

**Why `--jobs` matters:** GLM's per-task cost is `rate ÷ throughput`. One task at a time wastes ~7/8
of the GPU; running many in parallel shrinks the interval union and slashes $/task. The report's
break-even table shows the concurrency needed to beat Claude.

## Outputs
- `results/report.md` — the deliverable (numbers + cost analysis + break-even + blinded notes).
- `results/summary.csv` — per model: `success_rate`, tokens, `avg_duration_s`, `active_s`,
  `overlap_s`, `cost_per_successful_task`, `cost_basis`.
- `results/results_detailed.csv` — per (task,model,run): `start`, `end`, `duration_s`, tokens, cost.
- `results/<task>__<model>__runN/` — `output.log` (JSON transcript), `verify.log`, `usage.json`,
  `final_repo/` (the agent's edited code). `./clean.sh` wipes `results/`.

---

## Add a task
Easiest: run the **`/create-bench-task`** skill in Claude Code — it asks the right questions,
delegates to the `task-smith` agent to build + **validate** the fail→pass loop, and can run it.

Manually, a task is a dir under `tasks/<name>/`. The runner makes a fresh isolated copy of the
code per run, optionally runs `setup.sh`, runs the agent, then `verify.sh` (exit 0 = pass):

| File | Required | Purpose |
|---|---|---|
| `prompt.txt` | ✅ | instruction handed to the agent |
| `verify.sh` | ✅ | exit `0` = success; runs in the work-dir root |
| `setup.sh` | optional | runs before the agent (e.g. inject a bug); gets `$TASK_REPO_SRC` |
| `repo/` **or** `repo.path` **or** `repo.git` | one | self-contained code / local git repo / `<url> [ref]` remote |

Only `tasks/demo-*` are committed; other tasks stay local (gitignored). Keep verification
**objective and offline** (pytest, `dbt parse`, a compiled-SQL diff). For a *publishable*
benchmark use `repo.git` pinned to a tag/SHA, or SWE-bench Verified
(`python3 make_swebench_task.py <id>` — see [SWEBENCH.md](SWEBENCH.md)).

## Layout
```
setup.sh          # bring up the Modal GLM-5.2 auto-endpoint (idempotent)
run_bench.sh      # run task × model × run, then aggregate
aggregate.py      # manifest + usage.json -> summary.csv / results_detailed.csv
judge.py          # blinded LLM review + report.md (numbers, cost, break-even)
clean.sh          # wipe results/
opencode.jsonc    # provider config (secrets via {env:...})
tasks/demo-*/     # committed tasks; tasks/<other>/ are gitignored
results/          # logs + CSVs + report.md (gitignored)
```

## Gotchas
- **`opencode models` shows only `opencode/*`** → provider config not loaded; `run_bench.sh` sets
  `OPENCODE_CONFIG` automatically.
- **Anthropic/OpenAI 404 "Not Found"** → a stray `*_BASE_URL` env var (e.g. Claude Desktop's
  `ANTHROPIC_BASE_URL` without `/v1`). `run_bench.sh` and `judge.py` unset these.
- **GLM `$/task` looks huge** → the idle tax + batch-1. The 8×B200 bills ~$50/hr whenever up; run
  tasks densely / in parallel (`--jobs`) so the same GPU-hour covers more tasks. Turn the endpoint
  off when not benchmarking.
- **Modal billing lag** → GPU cost here comes from run timestamps × rate, so it's immediate (no
  waiting on Modal's ~1h billing settle).
- **`big-pickle`** is opencode's own hosted model, **not** the Modal GLM.
