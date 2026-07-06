# Prompt versions (the manifest)

Each task carries up to **three prompt versions** of the same underlying task, so the *phrasing*
becomes a measurable variable. Prompt version is a **first-class sweep dimension**: `./run_bench.sh`
runs every version present for every task, and every result row is tagged with it (`prompt` column,
threaded through `manifest.csv` → `summary.csv` → `report.md` and the complexity view).

**What each version does, in one line:**

| Version | What the prompt is | What its delta measures |
|---|---|---|
| **v1** | the terse, raw ask a developer would actually type — no setup hints, no success command | the floor (a minimally-engineered prompt) |
| **v2** | the shaped uniform template — structure + explicit success command + scope rules + env setup + checklist | `v2 − v1` = total value of prompt engineering |
| **v3** | control: v1's terse phrasing + *only* v2's operational bits (env setup + verify command), no structure | `v3 − v1` = operational context; `v2 − v3` = structure/discipline |

The rest of this file records **where each version comes from and what it's testing** — so a `v1`
vs `v2` vs `v3` row is interpretable.

## Convention

| File | Version label | Meaning |
|---|---|---|
| `tasks/<t>/prompt.v1.txt` | **`v1`** | the **default/baseline** prompt — the terse, unstructured ask |
| `tasks/<t>/prompt.v2.txt` | **`v2`** | the **shaped** uniform template (structure + explicit scope) |
| `tasks/<t>/prompt.<x>.txt` | **`<x>`** | any further variant (`v3`, …) |

The baseline is **`prompt.v1.txt`** — the minimal version a developer would actually type. The label
is the filename's version (`prompt.v1.txt` → `v1`; `prompt.v2.txt` → `v2`; `prompt.<x>.txt` → `<x>`).
The sweep runs them **in order v1 → v2 → v3**, so the report reads baseline-first.

## Running

```bash
./run_bench.sh                            # DEFAULT: runs every prompt.v*.txt present (v1, v2, v3), per task
./run_bench.sh --prompts prompt.v1.txt    # only v1 (baseline)
./run_bench.sh --prompts prompt.v2.txt,prompt.v3.txt   # a subset
```

Still grouped per model: within each `(harness, model)` group, `tasks × versions × runs` fire in
parallel; groups run one at a time (clean per-arm cost). So `v1` and `v2` of the same model are
separate rows and never pooled — you can read "v2 lifted success from X→Y" straight off the report.

## The versions

### `v1` — baseline (`prompt.v1.txt`)
**Source.** The minimal, unstructured ask.
- **Invented tasks** (injected-bug demos + the from-scratch build): a couple of sentences —
  symptom + "make the tests pass," or for the build task the functional/stack requirements stated
  plainly. No process coaching, no validation checklist, no explicit success command.
- **SWE-bench tasks**: the **original problem statement, verbatim** — the exact GitHub issue from
  the SWE-bench Verified dataset (`problem_statement`), with **nothing added** (no venv hint, no
  "make the tests pass," no scope rules). The raw issue as a developer would encounter it.

**What it tests.** The floor: how well each model does from a realistic, minimally-engineered
prompt — and, against `v2`, isolates the **prompt** as the only variable.

### `v2` — shaped uniform template (`prompt.v2.txt`)
**Source.** Colleague feedback: use ONE prompt shape for every task so the *prompt* stops being a
hidden variable. Sections: **Task** (for SWE, the issue embedded verbatim), **Success criteria**
(the exact verify command), **Scope** (smallest change, fix every occurrence, no refactors),
**Environment** (how to set up `.venv`), **Before finishing** (re-run the check).

**What it tests.** Whether explicit structure + a stated success command + tight scope raises
success rate and cuts wasted effort (fewer steps / tokens / backtracks), fairly (identical
scaffolding for every model). The delta `v2 − v1` is the value (or cost) of prompt engineering.

### `v3` — terse + operational scaffolding (`prompt.v3.txt`)
**Source.** A **control** to decompose the `v2 − v1` gain. It's the v1 phrasing (crude, no sections)
plus *only* the **operational context** v2 carries — how to set up the env (`.venv` + install) and
the **exact verify command** — but **not** v2's structure or scope discipline (no "smallest change /
fix every occurrence / no refactor", no checklist).

**What it tests.** Splits the improvement into two independent pieces:
- **`v3 − v1`** = value of *operational context* (telling the model how to set up and verify). For
  SWE this is roughly what the real SWE-bench harness provides for free (pre-built env + test command).
- **`v2 − v3`** = value of *structure + scope discipline* on top — the actual "prompt engineering."

If `v3 − v1` captures most of the gain, the v1→v2 story is "we supplied missing operational context,"
not clever wording. Present on all tasks (for the kanban build, the operational bit is the
`create-next-app` scaffold + the explicit build/test/API checks).

## Coverage

All six demo tasks carry `v1`, `v2`, and `v3`:

| Task | v1 (`prompt.v1.txt`) | v2 (`prompt.v2.txt`) | v3 (`prompt.v3.txt`) |
|---|---|---|---|
| `demo-median-bug`, `demo-click-parser`, `demo-slugify-lowercase` | terse "fix the failing tests" | shaped template | terse + env/verify cmd |
| `demo-swebench-psf__requests-6028` | raw dataset issue (verbatim) | issue + template scaffolding | raw issue + env/verify cmd |
| `demo-kanban-orchestration` | plain build request | shaped build spec (Task / Success criteria / Scope) | terse + create-next-app + build/test/API checks |

New SWE-bench tasks get all three automatically: `make_swebench_task.py` writes `prompt.v1.txt` (raw
statement), `prompt.v2.txt` (shaped), and `prompt.v3.txt` (raw issue + operational bits) side by side.

## Add a version

1. Write `tasks/<task>/prompt.<label>.txt` (do this for each task you want it compared on).
2. Add a **### `<label>`** entry above: its source and what it's testing.
3. Re-run — it's picked up automatically (or `--prompts` to target it). The report grows a row per
   `(harness, model, <label>)`.
