# SWE-bench Verified tasks (the gold-standard, publishable benchmark)

SWE-bench Verified is 500 **real** GitHub issues across popular OSS repos, each with a
hidden test suite. It's the standard coding-agent benchmark **and Claude has published
baselines on it** — so you can compare your GLM-on-Modal number against Anthropic's
official number, not one you made up.

## Generate a task from a real instance
`make_swebench_task.py` pulls a real instance from the dataset (base commit, test patch,
FAIL_TO_PASS test IDs, issue text — nothing fabricated) and writes a
`tasks/demo-swebench-<id>/` task you then run like any other.
```bash
pip install datasets
python3 make_swebench_task.py psf__requests-2317     # any real Verified instance id
./run_bench.sh --runs 1 --models "modal/zai-org/GLM-5.2-FP8 anthropic/claude-opus-4-8"
```
Start with **lightweight repos** (requests, flask, click, pytest) — fast install, offline.
Heavy scientific repos (numpy, scipy, astropy, scikit-learn) need big deps and are slow/flaky locally.

## What the generated task contains
- `repo.git` — the instance's repo pinned to its `base_commit`
- `setup.sh` — applies the dataset's **test patch** (introduces the failing tests)
- `verify.sh` — runs the instance's **FAIL_TO_PASS** tests (exit 0 = solved)
- `prompt.v1.txt` — the real GitHub issue text (`problem_statement`), verbatim; `prompt.v2.txt` — the
  same issue wrapped in our shaped template (both run by default — see PROMPTS.md)

## Comparing to Claude's baseline
- Official leaderboard + per-model resolved rates: <https://www.swebench.com>
- Anthropic publishes Claude's SWE-bench Verified score with each model release.
- Report **your** measured resolved-rate and $/solved-task next to those numbers.

## Rigorous runs
This is a lightweight local approximation (good for cost/latency + a quick resolved-rate on
easy repos). For a defensible leaderboard-grade number, use the **official SWE-bench
harness** (per-instance Docker images) and only swap the model.

> Not validated end-to-end here — it depends on the dataset, network, and each instance's
> own deps. Treat the first run of a new instance as a smoke test.
