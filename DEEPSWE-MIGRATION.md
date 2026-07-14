# deep-swe migration map

Pivot the benchmark from SWE-bench Verified → **deep-swe** (datacurve-ai), keeping our
Docker orchestrator + opencode/claude harnesses and the exact `report.html` +
`results-details.csv` output. Verified against a clone of the repo (not the README).

## Destination (done =)
`execute_bench.py` runs our harnesses against deep-swe tasks in Docker; a separate
grading step runs each task's upstream Harbor verifier and produces `resolved.json`
in the shape `aggregate.py` already reads; `report.html` + `results-details.csv`
generate unchanged. All 113 tasks, all 5 languages.

## Ground truth (verified from clone)
- **113 real tasks** (`tasks/` holds 113 task dirs + 4 dataset-level FILES: `dataset.toml`,
  `manifest.json`, `manifest.schema.json`, `README.md` — glob `tasks/*/task.toml` skips them).
  Langs: TS 35 · Python 34 · Go 34 · Rust 5 · JS 5. Clone pinned at `6db64a4`.
- Per task: `task.toml` (repo, base_commit, lang, **ECR docker_image**, cpu/mem/timeouts),
  `instruction.md`, `pre_artifacts.sh` (captures `git diff base..HEAD` → `model.patch`),
  `environment/Dockerfile`, `tests/{config.json, grader.py, test.sh, test.patch, Dockerfile}`,
  `solution/` (reference, unused in grading).
- Verifier is **self-contained upstream Python** (`grader.py` identical across tasks;
  per-task data is `config.json`). Grade formats: **78 ctrf + 35 junit**.
- `config.json`: `base_commit`, `f2p_node_ids`, `p2p_node_ids`, `grade{format,node_id,tool_label,reports}`.
- **`reward.json`** (grader output): `{reward:0|1, f2p_total, f2p_passed, p2p_total, p2p_passed, f2p, p2p, partial}`.
  `reward==1` iff `|f2p|>0` AND all f2p pass AND no p2p fails. **Maps 1:1 to `resolved.json.resolved`.**
- Every task's image is on **public ECR** (`public.ecr.aws/...`). `allow_internet=false` during
  BOTH agent env and verify — internet only needed at image-pull time.

## Decisions
- **A — Lang scope: DECIDED = all 5 langs / 113 tasks.**
- **B — Grade where: → Local docker verifier, as a separate host step (recommended).**
  Rationale: verifier is self-contained upstream code; grading runs no model → no billing
  impact → Modal offers nothing here; DinD is a non-issue because grading is a separate step
  off the host daemon (mirrors how `swe_eval_modal.py` is separate today).
- **C — Vendor mode: → git submodule, pinned (recommended).** Tasks are small text; we control
  bumps; reproducible. Offline is NOT a hard requirement (ECR pull needs net anyway).

### Agent-env decisions (Q1–Q6, gut-checked 2026-07-14)
Gut-check resolved the "are we reinventing Pier?" question: **no**. Pier measures model
*capability* (does the patch pass); we measure **cost per solved task**. Pier captures no
cost / no ccusage / no reasoning-tier billing → not a runner we can adapt. Build-our-own stands.
Kept everything that touches cost; downgraded the offline-fidelity work that does not.

- **Q1 = A — Harness INSIDE each task's prebuilt ECR image.** Repo at `/app`, `base_commit`
  checked out, toolchain baked in. `environment/Dockerfile` is only the recipe upstream used to
  build the ECR image — we pull the built image, don't rebuild it.
- **Q2 = A — Containerized orchestrator spawns per-task containers as SIBLINGS** on the host docker
  daemon via mounted `/var/run/docker.sock` (NOT docker-in-docker). Same mechanism for agent + verifier.
- **Q3 = keep all 3 GLM reasoning tiers** (modal default / modal-high / modal-nothink) + claude.
  The tiers ARE the cost dimension — dropping them kills the point of the benchmark.
- **Q4 = M3 — `reasoning_proxy.py` runs as its OWN sibling container** on a per-run docker network
  `bench-<runid>`; task containers join it; opencode reaches it by name. (Proxy is pure stdlib.)
- **Q5 = DOWNGRADED — no offline egress gateway.** Previous plan (`--internal` net + proxy as
  SOLE dual-homed egress gateway) was *fidelity theater* (mimic deep-swe `allow_internet=false`),
  not a cost measurement — cost numbers are identical whether the container is `--internal` or
  open. Ground truth already makes it fair without isolation: ECR image does `git remote remove
  origin`, and graded tests are challenge-specific (`TestChallenge...`), NOT the upstream repo's
  suite → browsing GitHub yields no answer key. **Decision:** task containers get normal egress
  (pull models directly); proxy stays as a plain sibling for reasoning injection, NOT a gateway.
  Drops most of the L/XL. Add real network isolation later only if a task proves it can cheat.
- **Q6 = A — mounted read-only node+CLI bundle. CONFIRMED.** Orchestrator copies its baked
  `/usr/local` (node 22 + opencode-ai@1.17.13 + @anthropic-ai/claude-code@2.1.193 + ccusage) into a
  shared volume once at boot, mounts it read-only into every task container.
  Verified facts: all 113 images derive from `mars-base` = **Debian 12 bookworm, glibc 2.36**;
  orchestrator base `node:22-bookworm-slim` = same glibc 2.36 → node binary runs as-is (ABI-safe,
  not assumed). Bundle = **527M** (node 117M + node_modules 403M). One bundle serves all 113.
  **PATH rule:** invoke the agent via absolute `/opt/agent/bin/node <cli>`; do NOT prepend the bundle
  to container `PATH` — 40 TS/JS task images ship their own node for the repo's build/test, and
  shadowing it would diverge from upstream grading.

- **Q8 = cost capture, CONFIRMED.** opencode/GLM token DB: mount a host dir as the container's
  `XDG_DATA_HOME`; opencode writes `…/opencode/opencode.db` straight onto the mount = the exact path
  `snapshot_usage()` (execute_bench.py:146) already reads; host-side ccusage (prefetched Dockerfile:46)
  unchanged. claude tokens ride `--output-format stream-json` on stdout → captured `output.log` →
  `claude_stats` SURVIVES Q1 untouched. GLM $ still from `billing.py` (Modal window).
  **Zero-token guard (fixes Risk #3 silent-$0):** if opencode exits 0 with tool activity in `output.log`
  but `usage.json` = 0 tokens → hard-FAIL the job, retry within existing budget; if still zero, mark run
  errored and EXCLUDE from cost aggregation. (An exit-0 opencode run always has ≥1 model call, so
  0 tokens = lost DB, never a legit empty.) Replaces the silent `{"sessions":[]}` writes at :150/:168.

- **Q9 = agent timeout, CONFIRMED = honor task.toml.** Default per-job timeout = `[agent].timeout_sec`
  (5400s for all 113); `--timeout` becomes an override cap for smoke tests. Rationale: cutting the agent
  off early undercounts tokens/cost AND depresses solve rate → biased vs deep-swe. It's a ceiling, so
  extra wall-time only hits stuck jobs. (Verifier `timeout_sec`=1800 applies to the separate grading step.)
- **Q10 = resource caps + cleanup, CONFIRMED.**
  Caps: `docker run --cpus 2 --memory 8192m` per container, read from task.toml (uniform 2 CPU / 8192 MB /
  20480 storage across all 113). Faithful OOM ceiling + host protection. Operator sizes `--jobs` so
  `jobs×2` CPU and `jobs×8GB` fit the host; add a startup oversubscription warning.
  Cleanup (Risk #1, replaces `killpg` at execute_bench.py:230): deterministic `--name bench-<runid>-<jid>`
  + `--label bench=<runid>`; timeout → `docker kill`; `finally` → `docker rm -f`; startup + shutdown sweep
  `docker rm -f $(docker ps -aq --filter label=bench=<runid>)`.

- **Q11 = networking, CONFIRMED.** Facts: only 2 tiers proxy (`modal-nothink`→off:8899,
  `modal-high`→high:8898); default `modal` tier and `claude` hit Modal/Anthropic DIRECTLY (opencode.jsonc
  baseURL). Topology: one user-defined bridge `bench-<runid>` per run — gives BOTH name-DNS to the proxy
  AND NAT egress to Modal/Anthropic (one network, since Q5 dropped isolation). **2 shared proxy sibling
  containers** (off/high) started once at run start, joined to the network; every task container joins it
  and reaches proxies by name. Proxies are stateless `ThreadingHTTPServer` → safe under parallel jobs.
  Required changes: rebind `reasoning_proxy.py:87` `127.0.0.1`→`0.0.0.0`; set
  `NOTHINK_ENDPOINT/HIGH_ENDPOINT=http://<proxy-name>:<port>/v1` and pass Modal/Anthropic creds into each
  task container via `-e` (replaces the `127.0.0.1` env at execute_bench.py:291). Cleanup: drop network +
  2 proxies at run end (folds into the Risk #1 `--label bench=<runid>` sweep).

- **Q13 = image lifecycle, CONFIRMED = pull-on-demand + prune-after-task.** Facts: agent image ~840MB
  compressed (~1–2GB on disk), anonymous pull confirmed, all 113 share mars-base base layers (3.42GB
  once); holding all 113 ≈ 150–200GB → exhausts Docker Desktop default disk. Pull each image when its
  first job starts; after ALL runs + grading for that task complete, `docker rmi` it (base layers stay
  shared). Bounds disk to active working set (~jobs × image) → runs on any host. `--keep-images` flag
  disables pruning for fast re-runs on big boxes. Prune is gated on generate+grade both done (grade
  needs the image too).

- **Q14 = prompt axis, CONFIRMED = collapse to instruction.md verbatim.** Feed `instruction.md` as the
  single prompt; `pv` column fixed to `"instr"` so aggregate.py/report unchanged. Rationale: 3× fewer
  runs; faithful (leaderboard agents get instruction.md); our `v2` success-command framing is meaningless
  since graded tests are hidden; instruction.md is already a shaped spec so wrapping double-shapes it.
  Drops the prompt-engineering sweep dimension (not the GLM-vs-Opus cost question). Retires
  `prompt.v1/2/3.txt` + PROMPTS.md path for deep-swe.

- **Q15 = vendoring, CONFIRMED = pinned submodule (decision C).** deep-swe as git submodule pinned at
  `6db64a4`; discovery globs `deep-swe/tasks/*/task.toml` (replaces the `prompt.v1.txt` filter at
  execute_bench.py:502). CI checkout needs `submodules: true`; Docker `COPY` needs it checked out at build.

- **Q16 = run flow, CONFIRMED = per-task interleave, --jobs-wide.** Restructure `main()` (currently
  two-pass: generate-all queue then one `grade()` at :642) into a worker pool of `--jobs` workers, each
  owning ONE task end-to-end: pull image → run its harness×model×run generate jobs → grade locally (the
  decision-B rewrite of `grade()` from Modal→local docker) → prune image (Q13). Preserves full Modal
  concurrency (N workers = N tasks generating at once → GPU saturated → min billing window; grading is
  CPU-only, overlaps other tasks' generation). Confirmed premise: **image source stays PULL prebuilt
  (Q1)** — disk peaks ~33GB at jobs=20 (shared mars-base layer + prune), NOT 150-200GB; pulling keeps
  deep-swe's frozen/offline/deterministic env vs build-from-Dockerfile's dep-drift + network flakiness.
  Note: GLM endpoint only needs to be alive during generation — can scale down before final-tail grades.

## Frontier tickets
1. **Task loader rewrite** — `execute_bench.py:prepare_work()` currently loads `repo.git`/`repo/`.
   *SUPERSEDED by Q1:* do NOT clone repo or build `environment/Dockerfile`. `docker pull` the prebuilt
   `task.toml [environment].docker_image` (`swe-bench-202605:<ext_id>-v1.1`); repo is already at `/app`,
   base_commit checked out. Loader = read `task.toml` → pull image → `docker run` (Q2). *Hard rewrite point.*
2. **Prompt source** — feed `instruction.md` as the task prompt (replaces `prompt.v1/2/3.txt` path). Verified: plain markdown task spec.
3. **Artifact capture (Q7 = O1, CONFIRMED)** — our agents edit the working tree WITHOUT committing;
   deep-swe's `pre_artifacts.sh` diffs only committed `base..HEAD` → would emit EMPTY patches (silent 0%).
   Fix: after agent finishes, in `/app` run `git -c user.email=bench@local -c user.name=bench add -A &&
   … commit -m bench`, THEN run the task's UNMODIFIED `pre_artifacts.sh` → `/logs/artifacts/model.patch`
   (its `--binary` + `safe.directory` = grader's exact input contract). Replaces `make_predictions.py`
   git-diff harvest for the deep-swe path. `/logs` must be a per-job mount so orchestrator reads the patch out.
4. **Grading step (Q12 = mount, CONFIRMED)** — `tests/Dockerfile` is only `<agent-image> + COPY`
   {test.sh,test.patch,grader.py,config.json} → `/tests` (reporter already baked into agent image).
   So DON'T build 113 verifier images: `docker run` the already-local agent image on a FRESH container
   (pristine `/app`, `environment_mode=separate`), `-v tests/:/tests:ro`, reuse the generate `/logs`
   mount (already holds `artifacts/model.patch`), `bash /tests/test.sh`, read `/logs/verifier/reward.json`.
   Runs no model → no billing impact. Applies the same `--label`/`rm -f` cleanup as Q10.
5. **reward.json → resolved.json adapter** — map `reward.json.reward` to
   `{instance_id, model_name_or_path, resolved}` so `aggregate.py` is untouched. **3-way, not 2-way:**
   `reward==1`→resolved; `reward==0`→unresolved; `reward.txt==-1` (test.sh infra-fail sentinel, no
   reward.json) → ERRORED → retry/exclude, NOT counted as a legit unresolved (mirrors the Q8 guard).
6. **Billing** — `billing.py` unchanged (measures model endpoint window, not grading).
7. **Vendor** — add deep-swe as pinned submodule; wire task discovery to `deep-swe/tasks/*`.
8. **Invocation contract + README** — the `docker run` command changes (Q2/Q13/Q15):
   - ADD `-v /var/run/docker.sock:/var/run/docker.sock` (Q2 sibling containers) — required or it can't
     spawn task containers. Also Dockerfile: add `docker-ce-cli` + solve socket uid/gid for `bench` (uid 1001).
   - DROP `-v "$PWD/.cache:/cache"` — no repo cloning; docker daemon caches images (Q1/Q13).
   - `--task` names are now deep-swe task_ids (e.g. `abs-module-cache-flags`), NOT `demo-swebench-*`.
   - Update README Quick Start, task list, and `make_swebench_task.py` mention accordingly.

## Open questions
- ~~4 non-task dirs in `tasks/`~~ RESOLVED: they're 4 metadata FILES, glob `tasks/*/task.toml` skips them. 113 tasks.
- junit vs ctrf: our adapter only needs `reward.json`, so grader.py handles both — no work for us.
- ~~ECR pull auth~~ RESOLVED: anonymous pull verified — pulled `mars-base` (acct x8v8d7g8) and inspected
  agent-image manifest (acct d3j8x8q7, ~840MB compressed) with no creds.
