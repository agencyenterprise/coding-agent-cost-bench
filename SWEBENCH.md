# SWE-bench Verified tasks (the gold-standard, publishable benchmark)

SWE-bench Verified is 500 **real** GitHub issues across popular OSS repos, each with a hidden test
suite. It's the standard coding-agent benchmark **and Claude has published baselines on it** — so you
can compare your GLM-on-Modal number against Anthropic's official number, not one you made up.

## Add a task from a real instance
`make_swebench_task.py` pulls a real instance from the dataset (base commit, test patch, FAIL_TO_PASS
test ids, issue text — nothing fabricated) and writes a `tasks/demo-swebench-<id>/` task.
```bash
python3 make_swebench_task.py psf__requests-6028     # any real Verified instance id
```
No `datasets` install needed — it falls back to the cached HF parquet via `pyarrow`.

## Two phases: generate locally, grade on Modal
The agent writes the fix on your machine (`bench.sh`), but grading runs in the cloud, inside each
instance's **official SWE-bench Docker image** (`swebench/sweb.eval.x86_64.<id>`, pulled as a Modal
Sandbox — no docker-in-docker). That image has the exact old Python + dependencies the project needs,
so **any Verified instance works** — django, sympy, pytest, sphinx, the heavy scientific repos —
nothing is limited by your host Python.

```bash
./run_auto_endpoint.sh --runs 3 --swe-grade     # generate on the AEP, then grade on Modal
./run_app.sh --tier 8xH200 --runs 3 --swe-grade # same, on a hand-rolled App
./grade_swe.sh --results-dir results/<run>       # grade a run you already have
```

Under the hood (`--swe-grade` chains these):
1. `make_predictions.py` — harvest each attempt's `model.patch` (the agent's `git diff`) → `predictions.jsonl`.
2. `swe_eval_modal.py` — run each patch in the instance's Modal image, run the project's tests, grade
   with SWE-bench's own parser → `resolved.json`.
3. `aggregate.py` — use `resolved.json` for the SWE pass/fail in the report.

## What a generated task contains
- `repo.git` — the instance's repo pinned to its `base_commit`
- `setup.sh` — applies the dataset's **test patch** (introduces the failing tests) during generation
- `test.patch`, `f2p.txt` — the dataset's test patch + FAIL_TO_PASS ids (reference; grading reads them from the dataset)
- `prompt.v1/v2/v3.txt` — the issue verbatim (v1), shaped template (v2), control (v3) — see PROMPTS.md

There is **no `verify.sh`** — SWE tasks are graded on Modal, never on the host.

## Validate a new instance before trusting it
Confirm the grader resolves the instance's **gold** patch on Modal (proves the image exists and the
repo's parser/tests work): build a one-line predictions file whose `model_patch` is the dataset's
`patch`, then `python3 swe_eval_modal.py --predictions gold.jsonl` — expect `RESOLVED`.

## Comparing to Claude's baseline
- Official leaderboard + per-model resolved rates: <https://www.swebench.com>
- Anthropic publishes Claude's SWE-bench Verified score with each model release.
- Report **your** measured resolved-rate and $/solved-task next to those numbers.
