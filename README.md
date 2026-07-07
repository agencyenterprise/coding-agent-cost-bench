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
./run_bench.sh                 # default matrix, auto-aggregates
```
Each entry is **`harness:model-ref`** (`harness` = `opencode` | `claude`). The same model can appear
under both harnesses — that's the point (model isolation vs real-world). Default matrix:
```
opencode:modal/zai-org/GLM-5.2-FP8           # GLM, default (max) reasoning
opencode:modal-high/zai-org/GLM-5.2-FP8      # GLM, reasoning_effort=high  (~45% fewer tokens)
opencode:modal-nothink/zai-org/GLM-5.2-FP8   # GLM, reasoning off
opencode:anthropic/claude-opus-4-8           # Opus, same harness as GLM (clean comparison)
claude:anthropic/claude-opus-4-8             # Opus in Claude Code's own CLI (real-world product comp)
```
The three `modal*` arms are a **reasoning sweep** (max / high / off) — run_bench starts one
`reasoning_proxy.py` per proxied tier (own port) so they run concurrently on the one endpoint.
`claude:` needs the `claude` CLI on PATH; it can't serve GLM/GPT/Gemini (Anthropic-only).

Common flags:

| Flag | Meaning | Default |
|---|---|---|
| `-r, --runs N` | repeats per (harness, model, task) | 1 |
| `-m, --models "a,b"` | comma/space list of `harness:model` | the matrix above |
| `--model H:REF` | add one entry (**repeatable**) | — |
| `-j, --jobs N` | max task×run jobs **in parallel within a group** | 30 |
| `-t, --tasks DIR` | tasks directory | `./tasks` |
| `--task NAME` | run **only** this task (dir name), e.g. `--task demo-kanban-orchestration` | all |
| `--prompts LIST` | restrict to these per-task prompt files (comma/space) | **all `prompt*.txt`** |
| `--delete-repo` | discard the mutated repo | keep |

**Every prompt version runs by default.** Each task holds `prompt.v1.txt` = `v1` (terse baseline / raw
issue), `prompt.v2.txt` = `v2` (shaped uniform template), and `prompt.v3.txt` = `v3` (terse + the
operational scaffolding v2 carries, a control to split style from operational context). The sweep
runs every version present (v1 first) and tags each result with it (`prompt` column,
threaded into `summary.csv` + `report.md` + the complexity view), so versions of the same model are
separate, comparable rows. See [PROMPTS.md](PROMPTS.md) for what each is, where it came from, and how
`v3 − v1` vs `v2 − v3` decompose the gain. `--prompts prompt.v1.txt` restricts to just the baseline.

**Parallelism is grouped.** Groups `(harness, model)` run **one at a time** so each arm's cost is
clean (no cross-arm contention inflating its latency); within a group every task×run fires **in
parallel** (up to `--jobs`), so each arm is measured at its own packing. The `modal*` arms are
adjacent in the matrix, so the GLM endpoint stays warm across them — sequential costs no re-cold-start.

Writes `results/manifest.csv` + per-run logs, then `aggregate.py` → `results/summary.csv` +
`results_detailed.csv`. Claude Code reports its own cost/usage/turns → those rows carry
`cost_basis = claude_code`; opencode API rows are `api_ccusage`; GLM is `gpu_calls`.

**Reasoning sweep (GLM max / high / off).** GLM-5.2 defaults to *max* reasoning while Opus runs with
none — which inflates GLM's tokens/cost. `run_bench` auto-starts a `reasoning_proxy.py` per proxied
tier (opencode can't add `chat_template_kwargs`; the Modal endpoint forwards it to SGLang — verified):
- `modal/…` → default/max (no proxy)
- `modal-high/…` → `reasoning_effort:high` (spike: ~45% fewer tokens, same answer)
- `modal-nothink/…` → `enable_thinking:false` (~99% fewer on a trivial task)

The default matrix already includes all three, so a plain `./run_bench.sh` runs the sweep. The open
question these arms answer: how much success do you lose as you dial reasoning down, vs the cost saved?

## 3. Judge
Turn the raw runs into the final report — numbers + a blinded LLM review of each transcript+diff:
```bash
python judge.py --judge gemini      # gemini | openai | anthropic | glm  (pick one NOT in the comparison)
```
Writes **`results/report.md`**: the numbers table, a **timeline** (start/end + overlap per model),
a **cost breakdown**, a **break-even table** (how many parallel tasks on Modal beat Claude), and
short, blinded per-task notes. All sections are generated from `summary.csv`, so re-running is safe.

## Run in Docker (optional — no host deps)
The committed tasks all run natively on a modern host (**Python 3.14** included). Docker is only for
avoiding local installs of opencode / Claude Code / node — or for running old SWE instances that need
an older Python (e.g. `pytest-dev/pytest-*`, see [SWEBENCH.md](SWEBENCH.md)). It bundles node +
opencode + Claude Code + Python 3.11 + git; the GLM endpoint stays on Modal.
```bash
cp .env.example .env && $EDITOR .env     # creds (used at runtime, never baked in)
./run_on_docker.sh --runs 1              # builds the image, runs the bench AND the judge
JUDGE=openai ./run_on_docker.sh --runs 3 # pick the judge; run_bench flags pass through
```
`results/` is mounted back to the host. See [Dockerfile](Dockerfile) — Claude Code runs headless via
`ANTHROPIC_API_KEY`.

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
- `results/summary.csv` — per (harness, model, **prompt version**): `success_rate`, tokens,
  `avg_duration_s`, `active_s`, `overlap_s`, `cost_per_successful_task`, `cost_basis`.
- `results/results_detailed.csv` — per (harness,model,**prompt**,task,run): `start`, `end`, `duration_s`, tokens, cost.
- `results/complexity.csv` — per task: **empirical complexity 0–10** (relative, from observed effort
  pooled across all models: steps, tool calls, output tokens, duration), `pass_rate`, and the raw
  averages. `report.md` merges this with an independent blind **LLM difficulty 1–5** per task.
- `results/<task>__<prompt>__<harness>_<model>__runN/` — `output.log` (transcript), `verify.log`, `usage.json`,
  `final_repo/` (the agent's edited code). `./clean.sh` wipes `results/`.

---

## Add a task
Easiest: run the **`/create-bench-task`** skill in Claude Code — it asks the right questions,
delegates to the `task-smith` agent to build + **validate** the fail→pass loop, and can run it.

Manually, a task is a dir under `tasks/<name>/`. The runner makes a fresh isolated copy of the
code per run, optionally runs `setup.sh`, runs the agent, then `verify.sh` (exit 0 = pass):

| File | Required | Purpose |
|---|---|---|
| `prompt.v1.txt` | ✅ | baseline instruction handed to the agent (version `v1`) |
| `prompt.v2.txt` | optional | shaped variant (version `v2`); add more as `prompt.<x>.txt` — see [PROMPTS.md](PROMPTS.md) |
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
clear_results.sh  # wipe results/
opencode.jsonc    # provider config (secrets via {env:...})
PROMPTS.md        # prompt-version registry: what v1/v2/... are + where they came from
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
