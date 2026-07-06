# Prompt versions (the manifest)

Each task can carry **more than one prompt version**, so the same fix can be handed to the agents
phrased differently. Prompt version is a **first-class sweep dimension**: `./run_bench.sh` runs
*every* version present for every task, and every result row is tagged with its version
(`prompt` column in `manifest.csv` → `summary.csv` → `report.md`). This file says **where each
version came from and what it's testing** — so a row labelled `v1` vs `v2` is interpretable.

## Convention

| File | Version label | Meaning |
|---|---|---|
| `tasks/<t>/prompt.txt` | **`v2`** | the canonical prompt (currently the uniform template) |
| `tasks/<t>/prompt.v1.txt` | **`v1`** | a named variant |
| `tasks/<t>/prompt.<x>.txt` | **`<x>`** | any further variant (`v3`, `terse`, …) |

The label is derived from the filename (`prompt.txt` → `v2`; `prompt.<x>.txt` → `<x>`). Adding a
version = dropping a `prompt.<label>.txt` file into a task dir and documenting it below.

## Running

```bash
./run_bench.sh                      # DEFAULT: runs every prompt*.txt present, per task
./run_bench.sh --prompts prompt.v1.txt          # only v1
./run_bench.sh --prompts prompt.txt,prompt.v1.txt   # just these two
```

Still grouped per model: within each `(harness, model)` group, `tasks × versions × runs` fire in
parallel; groups run one at a time (clean per-arm cost). So `v1` and `v2` of the same model are
separate rows and never pooled — you can read "v2 lifted success from X→Y" straight off the report.

## The versions

### `v2` — uniform structured template  (`prompt.txt`)
**Source.** Colleague feedback: use ONE prompt shape for every task instead of ad-hoc phrasing, so
the *prompt* stops being a hidden variable between tasks/models. Sections: **Task**, **Success
criteria** (the exact verify command), **Scope** (smallest change, fix every occurrence, no
refactors), **Environment** (how to set up `.venv`), **Before finishing** (re-run the check).

**What it tests.** Whether an explicit, structured prompt with a stated success command and tight
scope raises success rate and cuts wasted effort (fewer steps / tokens / backtracks) — and does so
*fairly*, since every model gets identical scaffolding. This is the current default.

### `v1` — terse baseline  (`prompt.v1.txt`)
**Source.** Our original hand-written prompts: a couple of sentences — symptom + "make the tests
pass" + "run them to confirm." No scope rules, no environment setup, no explicit command.

**What it tests.** The baseline for "how much does prompt engineering actually matter?" Holding
task/model/harness fixed, `v1` vs `v2` isolates the **prompt** as the only variable: the delta in
success rate and efficiency is the value (or cost) of the extra structure.

## Coverage

Not every task has every version — the sweep just runs whatever is present:

| Task | versions |
|---|---|
| `demo-median-bug`, `demo-click-parser`, `demo-slugify-lowercase` | `v1`, `v2` |
| `demo-swebench-pytest-dev__pytest-5787`, `-6197` | `v1`, `v2` |
| `demo-kanban-orchestration` | `v2` only |

## Add a version

1. Write `tasks/<task>/prompt.<label>.txt` (do this for each task you want it compared on).
2. Add a **### `<label>`** entry above: its source and what it's testing.
3. Re-run — it's picked up automatically (or `--prompts` to target it). The report grows a row per
   `(harness, model, <label>)`.
