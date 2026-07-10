---
name: create-bench-task
description: Add a task to the coding-agent cost benchmark in this repo (glm-review). Tasks are real SWE-bench Verified instances, graded on Modal (no host verify.sh). Use when the user wants to add/create a benchmark task or add a SWE-bench instance.
---

# Add a benchmark task

The benchmark is **SWE-bench Verified** instances: generated locally, then **graded on Modal** in each
instance's official Docker image. Adding a task = materialize an instance and confirm it grades. There
is **no host `verify.sh`** and no invented-bug injection. Read `SWEBENCH.md` for the full picture.

## 1. Pick an instance
Ask which instance (or criteria: repo, difficulty, Python era). **Any Verified instance works** — the
Modal grader supplies the exact environment, so old-Python / django / sympy / pytest / scientific repos
are all fine. To browse candidates, read the cached Verified parquet (path is in `make_swebench_task.py`)
and filter by `repo` and `difficulty` (`<15 min fix` … `>4 hours`) for a good spread.

## 2. Materialize it
```bash
python3 make_swebench_task.py <instance_id>      # e.g. psf__requests-6028
```
Writes `tasks/demo-swebench-<id>/`: `prompt.v1/v2/v3.txt`, `setup.sh`, `repo.git`, `test.patch`,
`f2p.txt`. No `verify.sh` — grading is on Modal.

## 3. Validate it grades (gold patch resolves on Modal)
The equivalent of the old fail→pass check, done in the correct environment: confirm the instance's
image exists and its **gold** patch resolves.
```bash
img="swebench/sweb.eval.x86_64.$(python3 -c "print('<id>'.replace('__','_1776_'))")"
docker manifest inspect "$img:latest" >/dev/null && echo "image ok"
# write a one-line predictions.jsonl whose model_patch is the dataset `patch`, then:
python3 swe_eval_modal.py --predictions gold.jsonl     # expect RESOLVED
```
If the image is missing or the gold patch doesn't resolve, pick a different instance.

## 4. Run it
```bash
./run_auto_endpoint.sh --runs 3 --task demo-swebench-<id> --swe-grade
```
Generates the patches, grades on Modal (`resolved.json`), and updates the report.

## Prompt versions (make_swebench_task.py emits all three — see PROMPTS.md)
- **`prompt.v1.txt`** — the raw GitHub issue, verbatim (nothing added).
- **`prompt.v2.txt`** — shaped uniform template: issue blockquote + a FILE-level suite command +
  scope / environment / checklist (same structure for every task, so phrasing isn't a confound).
- **`prompt.v3.txt`** — control: v1's terse phrasing + only v2's operational bits (env + verify command).

The default matrix runs `prompt.v2.txt`; pass `--prompts "prompt.v1.txt prompt.v2.txt prompt.v3.txt"`
for the full sweep.

## Guardrails
- Only `demo-*` tasks are git-tracked.
- A task isn't done until its **gold patch resolves on Modal** — never ship an instance you haven't
  confirmed grades.
