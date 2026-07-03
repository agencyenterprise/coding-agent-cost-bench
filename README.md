# Coding-agent cost benchmark

Runs the **same tasks** through the **same harness** ([opencode](https://opencode.ai))
across many models, then reports **success rate** and **cost per successful task** —
the honest metric, not raw $/token. Built for the Modal case study / DARPA DICE proposal.

## Why one harness
Comparing "Claude Code product" vs "GLM in opencode" mixes *model + harness*.
Running every model inside opencode isolates the **model** as the only variable.
(Product overhead can still be noted qualitatively in the write-up.)

## The two-currency cost rule
Every cost carries a `cost_basis` so it's auditable.
- **Claude / OpenAI / Gemini** → `cost = tokens × per-token price` (that's how the provider
  actually bills). From ccusage. `cost_basis = api_ccusage`.
- **GLM on Modal** → self-host, so you pay **GPU time** (the endpoint bills even while idle),
  not tokens. The honest number is the **real Modal spend for the benchmark window**,
  amortized: `cost/task = window_$ ÷ successful_tasks`. `cost_basis = gpu_billing`.
  `aggregate.py` fetches `window_$` automatically from `modal.billing` (needs `pip install
  modal && modal setup`). This captures idle and shrinks with `--jobs` — the crux of self-host vs API.

---

## 1. Prerequisites
- **opencode** installed (`opencode --version`).
- **python3** (tasks may need `pytest`, `dbt`, etc. — the agent installs those itself per task).
- **macOS:** `brew install coreutils` (for `gtimeout`).
- **Optional helpers:** `pip install -r requirements.txt` (`modal` for real GLM cost,
  `datasets` for SWE-bench) — then `modal setup` to auth. Not needed for a basic run.
- Provider credentials (next step).

## 2. Setup
Providers live in the **project-local** [opencode.jsonc](opencode.jsonc) (committed, no secrets —
uses `{env:...}`). `run_bench.sh` points opencode at it automatically via `OPENCODE_CONFIG`,
so it works even inside the /tmp clones. Nothing global is touched.

```bash
cp .env.example .env      # fill in the keys, then:
source .env
chmod +x run_bench.sh clean.sh tasks/*/verify.sh tasks/*/setup.sh
```

`.env` holds: `MODAL_ENDPOINT`, `MODAL_KEY`, `MODAL_SECRET`, `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`. Confirm the model IDs resolve:
```bash
opencode models | grep -E "modal|opus-4-8|fable|gpt-5-codex|gemini-2.5-pro"
```
Sanity-check the Modal endpoint answers:
```bash
opencode run "say hi" -m modal/zai-org/GLM-5.2-FP8
```

## 3. Run
```bash
./clean.sh
# smoke test: 1 run, GLM only
./run_bench.sh --runs 1 --models "modal/zai-org/GLM-5.2-FP8"

# full matrix: all models, 3 runs each (aggregates automatically at the end)
./clean.sh
./run_bench.sh --runs 3
```

### Flags (all long-form)
| Flag | Meaning | Default |
|---|---|---|
| `--runs N` | repeats per (task, model) — agents are stochastic | 3 |
| `--models "a b"` | space- or comma-separated model refs | built-in set |
| `--tasks DIR` | tasks directory | `./tasks` |
| `-j, --jobs N` | run up to N (task,model,run) jobs in parallel | 1 |
| `--timeout SECS` | kill a stuck/looping agent | 900 |
| `--retries N` | retries on opencode server error | 2 |
| `--delay SECS` | pause between sequential runs (`--jobs 1` only) | 2 |
| `--delete-repo` | discard the mutated repo (default keeps it in `results/<run>/final_repo`) | off |
| `--no-aggregate` | skip `aggregate.py` at the end | off |

`./run_bench.sh --help` prints this.

## 4. GLM cost — automatic
GLM cost = **real Modal spend for the window ÷ successful tasks**, fetched **automatically**
by `aggregate.py` from `modal.billing`. One-time setup:
```bash
pip install modal && modal setup     # install + auth; then run as usual
```
Basis becomes `gpu_billing`. Without modal installed/authed it shows `gpu_pending`
(API costs still compute).

## 5. Outputs
- `results/results_detailed.csv` — per (task,model,run): status, tokens, `cost_usd`,
  `cost_basis` (`api_ccusage` for APIs; GLM per-run is `gpu_amortized` — see summary).
- `results/summary.csv` — per model: `runs`, `passes`, `success_rate`, `avg_tokens_in/out`,
  `avg_duration_s`, `total_cost_usd`, **`cost_per_successful_task`**, `cost_basis`
  (`gpu_billing` for GLM, `api_ccusage` for APIs).
- `results/<task>__<model>__runN/` — per run: `output.log` (agent transcript, JSON),
  `verify.log`, `setup.log`, `usage.json`, and `final_repo/` (the agent's edited code —
  handy for eyeballing output quality; pass `--delete-repo` to skip and save disk).

## 6. Clean up
```bash
./clean.sh          # wipe results/
./clean.sh --all    # also report stray tmp.* work dirs (does not auto-delete)
```

---

## How to create a task

A task is a directory under `tasks/<name>/`. The runner, per run, makes a **fresh
isolated copy** of the code, optionally runs `setup.sh`, runs the agent, then `verify.sh`.

> **Easiest way:** run the `/create-bench-task` skill (in Claude Code) — it asks the
> right questions, delegates to the `task-smith` agent to build + **validate** the
> fail→pass loop, and can run it. The rest of this section is the manual reference.

> **Naming convention:** only `tasks/demo-*` are committed (see `.gitignore`). Name
> shareable/public tasks `demo-<something>`. Anything else (private-repo tasks, WIP)
> stays local and is git-ignored. Shipped demo tasks: `demo-median-bug` (self-contained),
> `demo-click-parser` + `demo-slugify-lowercase` (public repos). Local-only examples:
> `aepl-occurred-at-bug`, `dbt-macro-typo` (point at private repos).

| File | Required | Purpose |
|---|---|---|
| `prompt.txt` | ✅ | the instruction handed to the agent |
| `verify.sh` | ✅* | exit `0` = success. Runs in the work-dir root. |
| `setup.sh` | optional | runs before the agent (e.g. inject a bug). Gets `$TASK_REPO_SRC`. |
| `repo/` | one of these | self-contained starting code (copied fresh per run) |
| `repo.path` | one of these | abs path to a **local git repo** → `git clone --local` fresh per run |
| `repo.git` | one of these | **remote git repo** → one line `<url> [ref]` (ref = branch/tag/SHA), cloned fresh per run |

\* without `verify.sh` the status is `n/a` (no objective signal).

**Success must be objective and offline.** Prefer things that pass/fail without a DB or
warehouse: unit tests (`pytest`), `dbt parse`/`compile`, a compiled-SQL diff, a linter.

### Mode A — self-contained (`repo/`)
Ship the starting code inside the task. See [tasks/demo-median-bug](tasks/demo-median-bug).
```
tasks/my-task/
  prompt.txt          # "Fix the failing tests in ..."
  verify.sh           # runs pytest, exit code = pass/fail
  repo/               # the buggy code + its tests
```

### Mode B — external repo (`repo.path`)
Point at one of your real repos. The runner clones its committed HEAD fresh per run
(so uncommitted local edits don't leak in, and runs never contaminate each other).
```
tasks/my-task/
  prompt.txt
  verify.sh
  repo.path           # one line: /Users/you/dev/some-repo   (abs path; ~ ok)
```

### Mode C — injected bug on a real repo (`repo.path` + `setup.sh`) ← most reliable signal
`setup.sh` breaks something real in the fresh clone; the agent must fix it; `verify.sh`
confirms. Clean fail→pass signal, grounded in your actual code. `$TASK_REPO_SRC` is the
source repo path (handy to copy git-ignored bits like `dbt_packages/`).

Real examples in this repo:
- [tasks/aepl-occurred-at-bug](tasks/aepl-occurred-at-bug) — drops the UTC fallback in
  `ae-pl-api`'s `_coerce_to_datetime`; verify = `pytest tests/test_occurred_at_coerce.py`.
- [tasks/dbt-macro-typo](tasks/dbt-macro-typo) — renames a real macro to an undefined one
  in `pl_data_intel_dbt`; verify = `dbt parse`. `setup.sh` also copies `dbt_packages/` +
  `profiles.yml` from the source so parse works offline.

Minimal `setup.sh` (inject a bug by exact string swap — fail loudly if the anchor moved):
```bash
#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import pathlib
p = pathlib.Path("path/to/file.py")
t = p.read_text()
old, new = "the correct line", "the broken line"
assert old in t, "anchor not found — file changed; update setup.sh"
p.write_text(t.replace(old, new, 1))
print("injected bug")
PY
```

### Mode D — public remote repo (`repo.git`) ← best for a *publishable* benchmark
Same as Mode C but the source is a **public GitHub repo**, pinned to a tag/SHA for
reproducibility — so anyone can rerun and verify the numbers (ideal for the blog / DARPA).
Two shipped examples:
- [tasks/demo-click-parser](tasks/demo-click-parser): bug in `pallets/click` @ `8.1.7`
  (`split_arg_string` drops partial tokens); verify = `pytest tests/test_parser.py`.
- [tasks/demo-slugify-lowercase](tasks/demo-slugify-lowercase): bug in `un33k/python-slugify`
  @ `v8.0.4` (skips lowercasing); verify = `pytest test.py`.
```
tasks/demo-my-task/
  prompt.txt
  setup.sh            # inject the bug in the fresh clone
  verify.sh           # run the repo's tests
  repo.git            # one line:  https://github.com/owner/repo.git  <tag-or-sha>
```
For maximum credibility use **SWE-bench Verified** — real GitHub issues on public repos,
with **published Claude baselines**. Generate a task from a real instance:
```bash
pip install datasets
python3 make_swebench_task.py psf__requests-2317   # -> tasks/demo-swebench-<id>/
```
See [SWEBENCH.md](SWEBENCH.md).

> **Dep bootstrapping:** the fresh clone has no virtualenv. Either tell the agent in
> `prompt.txt` to create `.venv` and install what it needs (tests env-setup ability), or
> do it in `setup.sh` (isolates "can it code" from "can it install"). `run_bench.sh` and
> the sample `verify.sh` prefer `$work/.venv/bin/python` when present.

### Sourcing tasks
- Well-defined (objective): SWE-bench Verified instances, the Aider polyglot set, or
  injected bugs in your repos (Mode C).
- Open-ended (subjective): score with a blind 1–5 rubric; don't let these dominate the graph.

---

## Automation: the `/create-bench-task` skill + `task-smith` agent

Making a task by hand (clone → find a testable function → inject a bug → validate) is
repetitive, so it's codified into a **skill** (guided flow) and an **agent** (the worker).
Both are committed, so teammates get them on clone (`.claude/`).

### `/create-bench-task` — the skill (how to use)
In Claude Code, run the slash command or just ask in plain language:
```
/create-bench-task
# or: "add a task from pallets/flask @ 3.0.0, break url_for"
```
It (1) asks only what it still needs — source (public repo / local repo / self-contained /
SWE-bench), the repo + **pinned ref**, task kind, and name; (2) hands the build to the
`task-smith` agent; (3) offers to smoke-run the new task. Defined in
`.claude/skills/create-bench-task/SKILL.md`.
> A skill created mid-session only registers after creation — new sessions pick it up automatically.

### `task-smith` — the agent (how it works)
A subagent (`.claude/agents/task-smith.md`) that does the heavy lifting in its own context:
1. Clones the repo at the pinned ref into a **temp dir**; confirms the suite is green (baseline).
2. Finds a **pure function with direct test coverage** and crafts a **minimal string-swap**
   injection with an assert-anchor (fails loudly if the repo layout drifts).
3. Writes `tasks/<name>/{prompt.txt,verify.sh,setup.sh,repo.git|repo.path|repo/}` following the
   conventions (demo-* naming, offline verify, `.venv` bootstrap).
4. **Validates before returning:** clone → inject → `verify` FAILS → apply the fix →
   `verify` PASSES, reporting `N failed → M passed`.

It never touches your real repos (works in temp dirs) and **refuses to ship a task it
couldn't validate** — so nothing noisy lands in the benchmark. You can also invoke it
directly (Task tool / `@task-smith`) when you already know the repo and bug.

---

## Layout
```
opencode.jsonc          # provider config (committed, secrets via {env:...})
.env.example            # keys template -> copy to .env
run_bench.sh            # runs task × model × run; auto-aggregates
aggregate.py            # manifest + per-run usage.json -> cost CSVs (API=tokens, GLM=GPU)
clean.sh                # wipe results/
make_swebench_task.py   # generate a SWE-bench Verified task (see SWEBENCH.md)
tasks/demo-*/           # committed tasks: prompt.txt, verify.sh, [setup.sh], [repo/ | repo.path | repo.git]
tasks/<other>/          # any non-demo task -> git-ignored (private/local)
results/                # logs, ccusage snapshots, *.csv  (gitignored)
```

## Gotchas / troubleshooting
- **`opencode models` shows only `opencode/*`** → provider config not loaded. Ensure
  `OPENCODE_CONFIG` points to `opencode.jsonc` (run_bench sets this automatically).
- **`Unexpected server error` after the first run** → opencode state race when runs fire
  back-to-back. `--retries`/`--delay` mitigate; each run now uses an isolated
  `XDG_DATA_HOME`. Use `-j, --jobs N` for parallelism (watch API rate limits).
- **`results/*/output.log` is JSONL** — `run_bench.sh` uses `opencode run --format json --auto`
  (no TTY status spam; permissions auto-approved for unattended runs).
- **`big-pickle`** is opencode's own hosted model, **not** the Modal GLM — don't confuse
  its `$0.00` ccusage cost with GLM's.
- **GLM `$/task` looks huge** → the idle tax. The container bills even when no task runs,
  so the window's total spend ÷ few tasks is large. Run tasks densely / in parallel
  (`--jobs`) within one window so the same spend covers more tasks.
- **Idle cost is real** → the 8×B200 endpoint bills ~$52/hr whenever it's up (≈$1.25k/day
  24/7), regardless of tasks. Turn it off when not benchmarking.
- **GLM shows `gpu_pending`** → `pip install modal && modal setup` (aggregate.py fetches the
  real GPU $ from `modal.billing`); API costs compute without it.
- **Anthropic/OpenAI 404 "Not Found"** → a stray `*_BASE_URL` env var (e.g. Claude Desktop's
  `ANTHROPIC_BASE_URL` without `/v1`). `run_bench.sh` unsets these; if running opencode by
  hand, `unset ANTHROPIC_BASE_URL`.
