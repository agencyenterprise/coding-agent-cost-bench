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
python3 make_swebench_task.py psf__requests-6028     # any real Verified instance id
./run_bench.sh --runs 1 --models "modal/zai-org/GLM-5.2-FP8 anthropic/claude-opus-4-8"
```
No `datasets` install needed — it falls back to the cached HF parquet via `pyarrow`.

### Runs on your host (Python 3.14) — pick instances that do
The grader installs a **modern pytest** next to the repo, so a **pure-Python** instance runs on a
modern host (incl. **Python 3.14**) **iff its test files import cleanly there**. Guidance:
- ✅ **Good**: pure-Python, newest-version, small — e.g. `psf__requests-6028` (committed, validated
  fail→pass on 3.14). Prefer the highest `version` per repo.
- ❌ **Needs Docker (Python ≤3.11)**: `pytest-dev/pytest-*` — the package under test *is* pytest, so
  you can't swap in a modern one; old pytest crashes on 3.14 (`ast.Str` removed). Run via `./run_on_docker.sh`.
- ❌ **Skip**: `sympy/*` (FAIL_TO_PASS are bare test names, not pytest node ids → 0 tests collected);
  heavy C repos (numpy/scipy/scikit-learn/matplotlib) — slow deps, wheels may not build on 3.14.
- After generating, **validate the fail→pass loop on 3.14** before trusting it (clone, apply test
  patch → verify fails, apply the gold patch → verify passes).

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
