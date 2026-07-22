# deepswe-v1.1-33task

Frozen, committable result (light artifacts). The raw per-run logs stay under `runs/` (gitignored); regenerate them by re-running the benchmark.

| Setup | pass@k (tasks) | pass@1 (attempts) | $/attempt | $/completed task |
|---|---|---|---|---|
| opus | 24/33 | 60/132 | $6.68 | $36.72 |
| glm-default | 22/33 | 45/132 | $3.90 | $23.38 |
| glm-high | 23/33 | 56/132 | $1.96 | $11.27 |
| glm-nothink | 15/33 | 24/132 | $1.75 | $15.44 |

Files: `tasks.txt`, `manifest.csv`, `per_run.csv`, `summary.csv`, `deepswe_task_difficulty.csv`, `billing.json`, `progress_report.html`.

