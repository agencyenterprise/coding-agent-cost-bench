# Coding Agent Cost Benchmark

Compare coding agents on real GitHub issues using the metrics that matter: **success rate** and **cost per solved task**.

The benchmark runs the same tasks through each agent, grades every solution with the task's own upstream test suite, and generates a report comparing quality and cost.

Tasks come from the vendored **[deep-swe](deep-swe/)** dataset: **113 tasks across 5 languages** (Go, Python, JavaScript/TypeScript, Rust, …), each a real upstream issue with a prebuilt Docker image (repo at `/app`, base commit checked out) and a hidden verifier. Each task's sole prompt is its `instruction.md`, used verbatim.

Currently supported agents:

- GLM 5.2 (self hosted on Modal) — default, high, and no-think reasoning tiers
- Claude Opus

## Quick Start

```bash
cp .env.example .env
# Fill in your credentials

docker build -t glm-bench .   # always builds linux/amd64 (task images are amd64-only; emulated on arm hosts)

# Run one task with GLM
docker run --rm -it \
  --env-file .env \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/results:/out" \
  glm-bench \
  --models opencode:modal/zai-org/GLM-5.2-FP8 \
  --task abs-module-cache-flags \
  --runs 1

# Run the full benchmark (all tiers + Claude, across all 113 tasks)
docker run --rm -it \
  --env-file .env \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/results:/out" \
  glm-bench \
  --runs 4 \
  --jobs 20
```

The orchestrator runs each agent **inside the task's own image** as a sibling container on the host
Docker daemon — which is why it needs the docker socket mounted (`-v /var/run/docker.sock:/var/run/docker.sock`).
It never does docker-in-docker.

Results are written to `results/<timestamp>/`. Open `report.html` to view the summary.

## How it runs

Each task-worker owns one task end-to-end: it pulls the task's agent image, runs all of that task's
`(harness × model × run)` generate jobs, **grades each solution locally** in a fresh container off the
same image (the task's own `tests/` verifier — no model call, no cloud), then prunes the image so peak
disk stays bounded to roughly `--jobs × image`. Many tasks generate concurrently, keeping the Modal GPU
saturated. Pass `--keep-images` to skip the prune (useful when re-running the same tasks).

Grading maps the verifier's reward to a 3-way outcome: `reward == 1` → **resolved**, `reward == 0` →
**unresolved**, and a verifier infrastructure failure → **errored** (excluded from the resolve-rate and
cost, so it never counts as a false unresolved).

## Credentials

All credentials are loaded from `.env`.

Required for Modal (GLM endpoint + ground-truth billing):

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
| `--jobs` | Number of parallel task-workers |
| `--task` | Run a single task (e.g. `abs-module-cache-flags`) |
| `--models` | Comma separated list of models |
| `--keep-images` | Don't prune each task's agent image after grading |
| `--grade-only` | Regrade an existing results dir (local docker) |
| `--list` | List the discovered tasks and exit (`--list --task <id>` prints its prompt) |

Run `--help` to see all available options.

## Output

Each benchmark produces:

- `report.html` — success rate, cost per solved task, and cost comparison
- `summary.csv`
- `results_detailed.csv`
- `billing.json` — the actual Modal endpoint bill for the run window
- `resolved.json` — per-run grade (`resolved` / `unresolved`)

## Cost Model

API models are priced from their reported token usage. GLM uses the actual Modal endpoint cost during
the benchmark. The report normalizes everything into **cost per solved task**, making API and
self-hosted models directly comparable.

## Tasks

Tasks are vendored under [`deep-swe/tasks/`](deep-swe/tasks/) (a git submodule). List them:

```bash
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock glm-bench --list
```

Each task directory carries a `task.toml` (image, limits, timeouts), an `instruction.md` (the prompt),
and a `tests/` verifier used for grading. This benchmark consumes the dataset as-is; it does not author
new tasks.

## Project Layout

```
Dockerfile
execute_bench.py         Benchmark runner (generate → grade → report)
aggregate.py             Generate reports
billing.py               Fetch Modal billing
judge.py                 Optional LLM review
reasoning_proxy.py       GLM reasoning-tier control
setup_auto_endpoint.sh   Start Modal endpoint
deep-swe/tasks/          Benchmark tasks (submodule)
```
