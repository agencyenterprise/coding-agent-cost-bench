---
name: create-bench-task
description: Create a new task for the coding-agent cost benchmark in this repo (glm-review). Asks the right questions, builds the tasks/<name>/ files, validates the fail→pass loop, and optionally runs it. Use when the user wants to add/create a benchmark task, add a repo to the benchmark, or "make a new task".
---

# Create a benchmark task

Guide the user through adding one task to this benchmark, then delegate the build+validation
to the `task-smith` agent, then optionally run it. Read `README.md` and an existing task
(`tasks/demo-click-parser`) first if you need the current conventions.

## 1. Ask the right questions (only what's still unknown)
Use `AskUserQuestion`. Cover:
- **Source** — one of:
  - *Public remote repo* (`repo.git`, pinned tag/SHA) — best for a publishable/shareable task → name `demo-*`.
  - *Local/private repo* (`repo.path`) — internal signal only → NOT `demo-*` (stays git-ignored).
  - *Self-contained* (`repo/`) — you ship a tiny buggy snippet + its tests.
  - *SWE-bench Verified instance* — if so, don't hand-build; run `python3 make_swebench_task.py <instance_id>` instead and stop.
- **If a repo:** the URL (or path) and the **exact ref** (tag/SHA) to pin.
- **Task kind:** injected bug (recommended — clean fail→pass) vs an existing failing test vs a small feature.
- **Verification:** how success is checked, and confirm it runs **offline** (pytest / `dbt parse` / lint). If they don't know, let `task-smith` discover a well-tested pure function.
- **Name:** default `demo-<repo>-<short>`; confirm `demo-` if it should be committed.

If the user already gave enough (e.g. "add `pallets/flask` @ 3.0.0, break url_for"), skip
straight to step 2.

## 2. Delegate the build + validation
Spawn the **`task-smith`** agent with: the source (URL/path/ref or "self-contained"), the
task name, the kind, and any verification hint. It will find the injection point, write
`tasks/<name>/{prompt.txt,verify.sh,setup.sh,repo.git|repo.path|repo/}`, and **validate the
fail→pass loop** in a temp dir before returning. Relay its report (bug location, verify
command, `N failed → M passed`).

If `task-smith` reports it couldn't find a reliable offline injection point, tell the user
and suggest an alternative repo or a self-contained task — don't ship an unvalidated task.

## 3. Offer to run it
Ask if they want a smoke run now:
```bash
source .env
./run_bench.sh --runs 1 --tasks tasks --models "modal/zai-org/GLM-5.2-FP8" \
  # (or scope to the new task by temporarily pointing --tasks at a dir with just it)
```
To run only the new task, its dir can be isolated, or run the full set and read its rows
in `results/results_detailed.csv`.

## Guardrails
- Never commit secrets; public tasks use `repo.git` with `{env:...}`-free content only.
- Only `demo-*` tasks are git-tracked — respect the naming for shareable ones.
- A task without a **validated, offline** `verify.sh` is not done. If it can't be validated,
  say so rather than shipping noise into the benchmark.
