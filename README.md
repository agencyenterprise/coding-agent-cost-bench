# Coding Agent Cost Benchmark

Compare coding agents on the metric that matters for self-hosting: **cost per solved task**.

The benchmark runs the same contamination-free [DeepSWE](https://github.com/datacurve-ai/deep-swe)
tasks through each agent setup, grades every attempt with the task's own tests, and produces a
report comparing success rate and cost. It's built to weigh **self-hosted GLM-5.2 (on Modal, 8×B200)**
against **Claude Opus** across GLM's reasoning tiers.

## Results

Our study — **4 runs × 33 DeepSWE v1.1 tasks** — is in [`results/deepswe-v1.1-33task/`](results/deepswe-v1.1-33task/)
(task list, per-run CSVs, billing, and the rendered report). Headline, under the observed concurrency:

| Setup | pass@k (tasks solved) | $/attempt | $/completed task |
|---|---|---|---|
| Claude Opus 4.8 · Claude Code | 24/33 | $6.68 | $36.72 |
| GLM-5.2 · high reasoning · Modal | 23/33 | **$1.88** | **$10.77** |

GLM-5.2 (high) finished the same work at **~72% lower cost** while solving nearly as many tasks —
when the endpoint stays busy (a task alone on the GPU is ~$9). Full numbers and method in the
`results/deepswe-v1.1-33task/` folder.

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

## Pipeline at a glance

1. **Provision** the GLM auto-endpoint on Modal — once — with `./setup_auto_endpoint.sh`. *(GLM setups only.)*
2. **Run** the benchmark — one `docker run` — which writes raw per-run output + `manifest.csv` to `runs/<run-id>/`.
3. **Report** — `python3 benchmark_progress_report.py runs/<run-id>` — locally, producing `progress_report.html` + CSVs.

## 1. Provision the GLM endpoint (Modal)

The GLM setups send inference to a **GLM-5.2-FP8 auto-endpoint on Modal (8×B200)**. Provision it once:

```bash
pip install modal && modal setup          # authenticate the Modal CLI (writes ~/.modal.toml)
./setup_auto_endpoint.sh                  # idempotent: create the endpoint only if missing
```

`setup_auto_endpoint.sh`:
- ensures the Modal CLI is installed and authenticated (`modal setup`);
- enforces exactly **one proxy token** (`wk-…/ws-…`) that matches your `.env` — if none exists it
  creates one and prints the pair to copy into `.env`, then you re-run;
- creates the auto-endpoint for `zai-org/GLM-5.2-FP8`, **reusing the pre-downloaded weights volume**
  (`glm-5-2-weights`) so there's no multi-hundred-GB re-download;
- waits for provisioning (8×B200 cold start ≈ a few minutes);
- confirms `MODAL_ENDPOINT` — copy the endpoint URL from the [Modal dashboard](https://modal.com/endpoints/)
  and use `<url>/v1`.

It's safe to re-run any time (reuses an existing endpoint + volume). Options: `--name`, `--model`,
`--volume`, `--wait-tries` (see `./setup_auto_endpoint.sh --help`). An **Opus-only** run needs no Modal
endpoint — only `ANTHROPIC_API_KEY`.

## 2. Run the benchmark (`docker run`)

[pier](https://pypi.org/project/datacurve-pier/) drives the **host** Docker daemon (via the mounted
socket) to build and run each task's own container, inject the agent CLI into it, and grade it. The
image ships only python + the Docker CLI + pier + the orchestrator — the agent CLIs (opencode /
claude-code) are installed by pier inside each task container.

```bash
docker pull ghcr.io/agencyenterprise/coding-agent-cost-bench:latest

DIR="$PWD/runs"                          # host-aligned dir: holds runs + the pier job tree
docker run --rm -p 80:80 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$DIR:$DIR" -e OUT_DIR="$DIR" \
  -e HOST_IP="$(hostname -I | awk '{print $1}')" \
  --env-file .env \
  ghcr.io/agencyenterprise/coding-agent-cost-bench:latest \
  --setups glm-default,glm-high,glm-nothink,opus \
  --tasks ytt-jsonpath-query-api \
  --runs 4 --jobs 10
```

Raw per-run output lands under `runs/<run-id>/`: one folder per run
(`<setup>__<task>__runN/` with `output.log`, `reward.json`, `usage.json`, `model.patch`) plus
`manifest.csv`. Turn these into a report in step 3.

### Required `docker run` wiring

| Flag | Why |
|------|-----|
| `-v /var/run/docker.sock:/var/run/docker.sock` | pier builds & runs task containers on the host daemon |
| `-v "$DIR:$DIR" -e OUT_DIR="$DIR"` | the run **and** the pier job tree at a **host-aligned** path — see below |
| `-p 80:80` | publishes the reasoning-proxy sidecar so pier's egress proxy can reach it (only for `glm-high` / `glm-nothink`) |
| `-e HOST_IP=<box private ip>` | address the egress proxy uses to reach the sidecar (only for `glm-high` / `glm-nothink`) |
| `--env-file .env` | credentials (below) |

**Why the host-aligned `$DIR`:** pier runs *inside* this container but tells the *host* daemon to
bind-mount its job tree (under `$DIR/<run-id>/pier-jobs`) into each task container. Those paths must
exist at the same location inside and outside the container (docker-out-of-docker), so mounting `$DIR`
to the **same path** and pointing `OUT_DIR` at it keeps the run and the job tree aligned in one mount.

## 3. Generate the report (local)

Reporting is a **local** post-processing step — it reads the raw run folders, and pulls your real
Modal bill (which needs your Modal account token), so it's kept out of the benchmark image:

```bash
python3 benchmark_progress_report.py runs/<run-id>     # → progress_report.html + CSVs
python3 verify_report.py             runs/<run-id>     # correctness gate (recommended)
```

`benchmark_progress_report.py` is self-contained (stdlib + `modal` for the bill). `--no-billing` skips
the Modal bill pull and falls back to the modeled GPU rate. `verify_report.py` re-derives every number
from the raw files and fails loudly on any mismatch.

**Pulling from a remote box?** `./pull_run_from_server.sh [RUN_ID]` SSH-pulls the run dir and runs the
report in one step. Configure `REMOTE` / `REMOTE_RUNS` in `.env`.

### Output (written next to the run, in `runs/<run-id>/`)

- `progress_report.html` — **pass@k** (task solved in ≥1 run) + **pass@1** (per-run) success, and cost per solved task, per setup
- `per_run.csv` — one row per run: `orchestration_s` (full job) ⊇ `session_s` (agent session) ⊇ `generation_s` (GPU/API generation = cost basis), plus `billed_usd`/`sole_usd`, tokens, steps
- `summary.csv` — per-setup rollup
- `deepswe_task_difficulty.csv` — per-task complexity + pass rate
- `billing.json` — the actual Modal endpoint bill over the GLM-active window (unless `--no-billing`)

## Credentials

All from `.env` (`--env-file`); nothing is baked into the image (`.env` is git- and docker-ignored).
Copy `.env.example` and fill in:

| Var | For |
|-----|-----|
| `MODAL_ENDPOINT` | GLM auto-endpoint URL (`…/v1`) — GLM setups |
| `MODAL_KEY` / `MODAL_SECRET` | proxy token (`wk-…` / `ws-…`) authenticating requests **to** the endpoint — GLM setups |
| `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | Modal account token (`ak-…` / `as-…`) — provisioning + real billing |
| `ANTHROPIC_API_KEY` | Claude Opus — `opus` setup |
| `REMOTE` / `REMOTE_RUNS` | *(optional)* SSH target for `pull_run_from_server.sh` |

Create the **proxy token** with `modal workspace proxy-tokens create` (or let `setup_auto_endpoint.sh`
print one). Create the **account token** at [modal.com/settings/tokens](https://modal.com/settings/tokens)
or `modal token new`.

## Reasoning tiers

`reasoning_proxy.py --router` runs as a sidecar on port 80 inside the container. For `glm-high` and
`glm-nothink`, opencode's `baseURL` points at `http://$HOST_IP/<tier>/v1`; the proxy strips the
`/<tier>` prefix, injects the matching `chat_template_kwargs` (`reasoning_effort:high` or
`enable_thinking:false`), and forwards to `MODAL_ENDPOINT`. `glm-default` talks to the endpoint
directly (no proxy). A single sidecar serves every tier by URL path because the task egress proxy
only permits ports 80/443.

## Common flags (`run_deepswe.py`, passed after the image name)

| Flag | Description |
|------|-------------|
| `--setups` | comma list: `glm-default,glm-high,glm-nothink,opus` |
| `--tasks` | comma list of task names, or `all` |
| `--runs` | attempts per (setup, task) |
| `--jobs` | parallel pier runs |
| `--run-id` | reuse/resume a run folder — skips runs already recorded in its `manifest.csv` |
| `--timeout-mult` | scale pier's agent timeout (`1.0` = full; smaller = faster smoke) |
| `--list-tasks` | print the baked DeepSWE task names and exit |

## Cost model

- **Claude Opus** — priced from reported token usage.
- **GLM (self-hosted)** — the **actual** Modal bill for the auto-endpoint over the GLM-active window
  (`billing.json`), split across GLM runs by **concurrency**: each generating second is shared among
  the runs in flight then, so the per-setup totals reconcile to the real bill (never double-counted
  for parallelism). Without `billing.json` it falls back to a modeled hourly rate.

The report normalizes everything to **cost per solved task**, making the API model and the
self-hosted tiers directly comparable.

## Project layout

```
Dockerfile                    one-`docker run` benchmark image (orchestrator + sidecar)
entrypoint.sh                 starts the reasoning-proxy sidecar, then the orchestrator
run_deepswe.py                orchestrator: fans pier over (setup × task × run) → raw runs + manifest.csv
reasoning_proxy.py            GLM reasoning-tier sidecar (router mode)
setup_auto_endpoint.sh        provision the GLM-5.2 Modal auto-endpoint (idempotent)
benchmark_progress_report.py  the report (run locally): pass@k / pass@1 + cost, real bill split by concurrency
verify_report.py              correctness gate for the report's numbers
pull_run_from_server.sh       pull a run from the server, then run the report
create_result.sh              name a run (runs/<name>/, local) and freeze it into results/<name>/
results/<name>/               committed result: task list + CSVs + billing + rendered report
runs/                         local scratch — raw per-run runs (gitignored)
```
