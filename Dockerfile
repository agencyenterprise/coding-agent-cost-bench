# glm-review benchmark — reproducible CPU orchestrator. One `docker run` does generate -> grade ->
# report.html. The GLM GPU (Modal AEP) and SWE grading (Modal sandboxes) stay in the cloud; this
# image never needs a GPU or docker-in-docker.
#
#   docker build -t glm-bench .
#   docker run --rm -v "$PWD/results:/out" \
#     -e MODAL_ENDPOINT -e MODAL_KEY -e MODAL_SECRET -e ANTHROPIC_API_KEY \
#     -e MODAL_TOKEN_ID -e MODAL_TOKEN_SECRET glm-bench --runs 1 --jobs 4
#
# Creds come ONLY from the environment at RUNTIME (never baked, never a mounted file): pass -e (as
# above, forwarding from your shell) or --env-file with a plain KEY=value file. Grading always runs;
# it needs the Modal CLI token (MODAL_TOKEN_ID/MODAL_TOKEN_SECRET). See execute_bench.py.
FROM node:22-bookworm-slim

# python 3.11 (bookworm) is a broad-compat sweet spot for the task repos — deliberately NOT the host's
# 3.14 that broke old libs. git for clones; coreutils gives a real `timeout`; sqlite3 for ccusage.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git curl ca-certificates coreutils perl sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# pinned agent CLIs (the "setups": opencode drives GLM, Claude Code drives Opus)
RUN npm i -g opencode-ai@1.17.13 @anthropic-ai/claude-code@2.1.193

# python tooling prebaked so nothing pip-installs mid-benchmark: aggregate/billing (modal) and the
# official SWE-bench grader (swebench + pyarrow + datasets). grade_swe.sh reuses this via the symlink.
ENV VENV=/opt/venv
RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install -q --upgrade pip \
    && "$VENV/bin/pip" install -q modal swebench pyarrow datasets rich python-dotenv psutil
ENV PATH="$VENV/bin:$PATH"

# non-root user — `claude --dangerously-skip-permissions` refuses to run as root
RUN useradd -m -u 1001 bench \
    && printf '{"hasCompletedOnboarding":true}\n' > /home/bench/.claude.json

WORKDIR /app
COPY . /app
RUN mkdir -p /app/.cache /out /cache \
    && ln -sfn /opt/venv /app/.cache/swe-venv \
    && chmod +x /app/*.sh \
    && chown -R bench:bench /app /out /cache

USER bench
# /out = results, /cache = cloned task repos. Mount host dirs to keep either across runs.
ENV OPENCODE_CONFIG=/app/opencode.jsonc NO_COLOR=1 OUT_DIR=/out CACHE_DIR=/cache
RUN npx -y ccusage --help >/dev/null 2>&1 || true   # prefetch ccusage so per-run usage.json is clean
# Bake the SWE-bench Verified parquet into the image's HF cache (as bench, so ~ = /home/bench) — the
# grader reads test patches + FAIL_TO_PASS from it, and the container has no host HF cache to borrow.
RUN python3 -c "from huggingface_hub import snapshot_download; \
    snapshot_download(repo_id='princeton-nlp/SWE-bench_Verified', repo_type='dataset')"

VOLUME ["/out", "/cache"]
ENTRYPOINT ["/opt/venv/bin/python3", "/app/execute_bench.py"]
