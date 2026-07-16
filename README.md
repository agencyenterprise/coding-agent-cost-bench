# Coding Agent Cost Benchmark

Compare coding agents on the metric that matters for self-hosting: **cost per solved task**.

The benchmark runs the same contamination-free [DeepSWE](https://github.com/datacurve-ai/deep-swe)
tasks through each agent setup, grades every attempt with the task's own tests, and generates a
report comparing success rate and cost. It's built to weigh **self-hosted GLM-5.2 (on Modal, 8×B200)**
against **Claude Opus** across GLM's reasoning tiers.

## Setups

| Setup | Harness | Model / tier |
|-------|---------|--------------|
| `glm-default` | opencode | GLM-5.2 FP8 — **max** reasoning (the default) |
| `glm-high`    | opencode | GLM-5.2 FP8 — **high** reasoning (`reasoning_effort:high`) |
| `glm-nothink` | opencode | GLM-5.2 FP8 — **no** reasoning (`enable_thinking:false`) |
| `opus`        | claude-code | Claude Opus 4.8 |

The three GLM tiers are the only ones GLM-5.2's chat template distinguishes: `reasoning_effort` is
either `high` or `max` (max = the default when unset), plus `enable_thinking:false` for no reasoning.
opencode can't set `chat_template_kwargs` itself, so a small reasoning-proxy sidecar injects the tier
and forwards to the endpoint (see [Reasoning tiers](#reasoning-tiers)).

## How it runs

Everything is one `docker run`. [pier](https://pypi.org/project/datacurve-pier/) drives the **host**
Docker daemon (via the mounted socket) to build and run each task's own container, inject the agent
CLI into it, and grade it. The image ships only python + the Docker CLI + pier + the orchestrator —
the agent CLIs (opencode / claude-code) are installed by pier inside each task container.

```bash
docker pull ghcr.io/agencyenterprise/coding-agent-cost-bench:latest

sudo mkdir -p /work && sudo chmod 777 /work        # host-aligned pier job tree (see note below)

docker run --rm -p 80:80 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /work:/work \
  -v "$PWD/results:/out" \
  -e HOST_IP="$(hostname -I | awk '{print $1}')" \
  --env-file .env \
  ghcr.io/agencyenterprise/coding-agent-cost-bench:latest \
  --setups glm-default,glm-high,glm-nothink,opus \
  --tasks ytt-jsonpath-query-api \
  --runs 4 --jobs 8
```

Results land in `results/`; open `report.html`.

### Required `docker run` wiring

| Flag | Why |
|------|-----|
| `-v /var/run/docker.sock:/var/run/docker.sock` | pier builds & runs task containers on the host daemon |
| `-v /work:/work` | pier's job tree at a **host-aligned** path — see below |
| `-v "$PWD/results:/out"` | where `report.html` + CSVs are written |
| `-p 80:80` | publishes the reasoning-proxy sidecar so pier's egress proxy can reach it (only needed for `glm-high` / `glm-nothink`) |
| `-e HOST_IP=<box private ip>` | address the egress proxy uses to reach the sidecar (only for `glm-high` / `glm-nothink`) |
| `--env-file .env` | credentials (below) |

**Why the host-aligned `/work`:** pier runs *inside* this container but tells the *host* daemon to
bind-mount its job tree into each task container. Those paths must exist at the same location inside
and outside the container, so `/work` must map to a host `/work` (docker-out-of-docker). `/out` needs
no alignment — only the orchestrator touches it.

## Credentials

All from `.env` (`--env-file`); nothing is baked into the image. Copy `.env.example` and fill in:

```
MODAL_ENDPOINT        # GLM auto-endpoint URL (…/v1)      — GLM setups
MODAL_KEY             # proxy token (wk-…)                — GLM setups
MODAL_SECRET          # proxy token (ws-…)                — GLM setups
MODAL_TOKEN_ID        # Modal account token (ak-…)        — real billing
MODAL_TOKEN_SECRET    # Modal account token (as-…)        — real billing
ANTHROPIC_API_KEY     # Claude Opus                       — opus setup
```

The GLM endpoint must already be running. Provision it once with `./setup_auto_endpoint.sh` (needs
the Modal CLI authenticated); it's idempotent and reuses the pre-downloaded weights volume.

## Reasoning tiers

`reasoning_proxy.py --router` runs as a sidecar on port 80 inside the container. For `glm-high` and
`glm-nothink`, opencode's `baseURL` points at `http://$HOST_IP/<tier>/v1`; the proxy strips the
`/<tier>` prefix, injects the matching `chat_template_kwargs` (`reasoning_effort:high` or
`enable_thinking:false`), and forwards to `MODAL_ENDPOINT`. `glm-default` talks to the endpoint
directly (no proxy). A single sidecar serves every tier by URL path because the task egress proxy
only permits ports 80/443.

## Common flags

| Flag | Description |
|------|-------------|
| `--setups` | comma list: `glm-default,glm-high,glm-nothink,opus` |
| `--tasks` | comma list of task names, or `all` |
| `--runs` | attempts per (setup, task) |
| `--jobs` | parallel pier runs |
| `--timeout-mult` | scale pier's agent timeout (`1.0` = full; smaller = faster smoke) |
| `--no-billing` | skip the real Modal bill pull (report uses the modeled GPU rate) |
| `--list-tasks` | print the baked DeepSWE task names and exit |

## Output

Written to `results/` (i.e. the mounted `/out`):

- `report.html` — success rate + cost per solved task per setup
- `per_run.csv` — one row per run (task, setup, passed, cost, tokens, steps)
- `summary.csv` — per-setup rollup
- `billing.json` — the actual Modal endpoint bill over the run window (unless `--no-billing`)

## Cost model

- **Claude Opus** — priced from reported token usage.
- **GLM (self-hosted)** — generation-seconds × the endpoint's hourly rate, reconciled against the
  **actual** Modal bill for the auto-endpoint over the run window (`billing.json`).

The report normalizes everything to **cost per solved task**, making the API model and the
self-hosted tiers directly comparable.

## Project layout

```
Dockerfile              one-`docker run` image
entrypoint.sh           starts the reasoning-proxy sidecar, then the orchestrator
run_deepswe.py          orchestrator: fans pier over (setup × task × run), then reports
reasoning_proxy.py      GLM reasoning-tier sidecar (router mode)
benchmark_progress_report.py  the single report — pass@k + per-run cost, real bill split
                              by concurrency, Modal billing refresh (run locally on results/)
verify_report.py        correctness gate for the report's numbers (run before trusting a report)
setup_auto_endpoint.sh  provision the GLM Modal auto-endpoint
```
