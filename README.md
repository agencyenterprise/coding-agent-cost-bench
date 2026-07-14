# Coding Agent Cost Benchmark

Compare coding agents on real GitHub issues using the metrics that matter: **success rate** and **cost per solved task**.

The benchmark runs the same SWE-bench Verified tasks through each agent, grades every solution with the project's own test suite, and generates a report comparing quality and cost.

Currently supported:

- GLM 5.2 (self hosted on Modal)
- Claude Opus

## Quick Start

```bash
cp .env.example .env
# Fill in your credentials

docker build -t glm-bench .

# Run one task with GLM
docker run --rm -it \
  --env-file .env \
  -v "$PWD/results:/out" \
  glm-bench \
  --models opencode:modal/zai-org/GLM-5.2-FP8 \
  --task demo-swebench-psf__requests-6028 \
  --runs 1

# Run the full benchmark
docker run --rm -it \
  --env-file .env \
  -v "$PWD/results:/out" \
  glm-bench \
  --runs 4 \
  --jobs 20
```

Results are written to:

```
results/<timestamp>/
```

Open `report.html` to view the summary.

Add `-v "$PWD/.cache:/cache"` to reuse cloned task repos across runs (skips re-cloning each time).

## Credentials

All credentials are loaded from `.env`.

Required for Modal:

```
MODAL_TOKEN_ID
MODAL_TOKEN_SECRET
MODAL_KEY
MODAL_SECRET
```

Required for Claude:

```
ANTHROPIC_API_KEY
```

## Common Flags

| Flag | Description |
|------|-------------|
| `--runs` | Attempts per task |
| `--jobs` | Number of parallel workers |
| `--task` | Run a single task |
| `--models` | Comma separated list of models |
| `--grade-only` | Regrade an existing run |

Run `--help` to see all available options.

## Output

Each benchmark produces:

- `report.html` — success rate, cost per solved task, and cost comparison
- `summary.csv`
- `results_detailed.csv`
- `billing.json`

## Cost Model

API models are priced from their reported token usage.

GLM uses the actual Modal endpoint cost during the benchmark.

The report normalizes everything into **cost per solved task**, making API and self hosted models directly comparable.

## Add a Task

```bash
python make_swebench_task.py psf__requests-6028
```

This imports any SWE-bench Verified instance into `tasks/`.

## Project Layout

```
Dockerfile
execute_bench.py         Benchmark runner
aggregate.py             Generate reports
billing.py               Fetch Modal billing
judge.py                 Optional LLM review
make_predictions.py      Build predictions.jsonl
make_swebench_task.py    Import SWE-bench tasks
reasoning_proxy.py       GLM reasoning control
setup_auto_endpoint.sh   Start Modal endpoint
swe_eval_modal.py        Grade solutions on Modal
tasks/                   Benchmark tasks
```