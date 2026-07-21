# Study: GLM-5.2 on Modal vs Claude Opus — 33 DeepSWE v1.1 tasks

Frozen results for the cost-per-solved-task comparison. **4 runs × 33 tasks** per setup
(132 attempts each), graded by each task's own tests. GLM-5.2-FP8 on an 8×B200 Modal Auto
Endpoint (three reasoning tiers) vs Claude Opus 4.8 in Claude Code.

## Headline (busy endpoint, cost split across concurrent tasks)

| Setup | pass@1 (per attempt) | pass@k (task solved in ≥1 run) | $/attempt | $/completed task |
|---|---|---|---|---|
| Claude Opus 4.8 · Claude Code | 60/132 (45.5%) | 24/33 (72.7%) | $6.68 | $36.72 |
| GLM-5.2 · max (default) | 45/132 (34.1%) | 22/33 (66.7%) | $3.06 | $18.38 |
| GLM-5.2 · high reasoning | 56/132 (42.4%) | 23/33 (69.7%) | **$1.88** | **$10.77** |
| GLM-5.2 · no thinking | 24/132 (18.2%) | 15/33 (45.5%) | $1.69 | $14.86 |

GLM-high averaged **$1.88/attempt vs Opus $6.68 — ~72% cheaper** under the observed
concurrency. Priced as if a task had the GPU alone, GLM is ~$9/attempt (pricier than Opus) —
utilization is what flips it.

## Cost basis

- **Opus** — reported token usage × published API prices (per-token; `billed == sole`).
- **GLM** — the **actual Modal endpoint bill**, split across concurrent runs. The endpoint bill
  over the GLM-active window was **$875.07 (16.63 h at ~$52.63/hr)**; `per_run.csv billed_usd`
  sums to it. `sole_usd` is the same run priced alone (no sharing) — always ≥ `billed_usd`.
- Idle-but-up time (endpoint warm, no GLM request) is **not** charged to any task.

## Files

| File | What |
|---|---|
| `tasks.txt` | the 33 task names (first 18 in list order + 15 sampled) |
| `manifest.csv` | one row per run: model, task, run, status, start/end |
| `per_run.csv` | per run: `orchestration_s ⊇ session_s ⊇ generation_s`, `billed_usd`/`sole_usd`, steps, tokens |
| `summary.csv` | per-setup rollup (runs, passes, rates, costs) |
| `deepswe_task_difficulty.csv` | per-task complexity + pass rate |
| `billing.json` | the real Modal endpoint bill, prorated to GLM-active time (hour by hour) |
| `progress_report.html` | the rendered report (pass@k / pass@1 tables + cost) |

Raw per-run agent transcripts (`output.log`, patches) are not committed here — regenerate them
by re-running the benchmark, or ask for the archive.

## Reproduce

```bash
TASKS=$(grep -v '^#' tasks.txt | paste -sd, -)
docker run --rm ... ghcr.io/agencyenterprise/coding-agent-cost-bench:latest \
  --setups glm-default,glm-high,glm-nothink,opus --tasks "$TASKS" --runs 4 --jobs 10
python3 benchmark_progress_report.py results/<run-id>   # -> these CSVs + report
```

See the repo root README for full setup (Modal endpoint, credentials, wiring).
