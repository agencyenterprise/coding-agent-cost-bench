# glm-review benchmark — reproducible CPU orchestrator. One `docker run` does generate -> grade ->
# report.html. The GLM GPU (Modal AEP) and SWE grading (Modal sandboxes) stay in the cloud; this
# image never needs a GPU. It spawns per-task work as SIBLING containers on the HOST docker daemon
# (Q2), so it needs the docker CLI + the host socket mounted in — NOT docker-in-docker.
#
#   docker build -t glm-bench .
#   docker run --rm -v "$PWD/results:/out" \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -e MODAL_ENDPOINT -e MODAL_KEY -e MODAL_SECRET -e ANTHROPIC_API_KEY \
#     -e MODAL_TOKEN_ID -e MODAL_TOKEN_SECRET glm-bench --runs 1 --jobs 4
#
# The `-v /var/run/docker.sock:...` mount is REQUIRED — without it the orchestrator can't spawn task
# containers. The entrypoint (running as root) grants the `bench` user access to that socket's group,
# then drops to `bench`, so `docker ps` works as bench with no extra --group-add flag.
#
# Creds come ONLY from the environment at RUNTIME (never baked, never a mounted file): pass -e (as
# above, forwarding from your shell) or --env-file with a plain KEY=value file.
FROM node:22-bookworm-slim

# python 3.11 (bookworm) is a broad-compat sweet spot for the task repos — deliberately NOT the host's
# 3.14 that broke old libs. git for clones; coreutils gives a real `timeout` + `stat`; sqlite3 for
# ccusage; gosu to drop root->bench in the entrypoint after fixing the socket group. Then add Docker's
# apt repo and install `docker-ce-cli` (client only) so the orchestrator can pull/run/exec/kill/rm.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git curl ca-certificates coreutils perl sqlite3 gosu \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# pinned agent CLIs (the "setups": opencode drives GLM, Claude Code drives Opus) + ccusage token reader
RUN npm i -g opencode-ai@1.17.13 @anthropic-ai/claude-code@2.1.193 ccusage

# Read-only node+CLI bundle (Q6): a self-contained copy of the node runtime + the pinned CLIs. Later
# tickets mount this into every task container read-only. mars-base (all 113 task images) is Debian 12
# / glibc 2.36, same as this base, so the node binary is ABI-compatible. Invoke agents via the ABSOLUTE
# `/opt/agent/bin/node <cli>` — do NOT put /opt/agent/bin on PATH, or a mount would shadow a task
# image's own node (40 TS/JS images ship their own for build/test). ~527 MB.
RUN cp -a /usr/local /opt/agent

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

# HOME is set explicitly so gosu (which preserves env) drops into /home/bench, not root's home.
ENV OPENCODE_CONFIG=/app/opencode.jsonc NO_COLOR=1 OUT_DIR=/out CACHE_DIR=/cache HOME=/home/bench
# prefetch ccusage as bench so per-run usage.json is clean and the cache lands in /home/bench
USER bench
RUN npx -y ccusage --help >/dev/null 2>&1 || true
# back to root: the entrypoint must adjust the socket group before dropping to bench
USER root

VOLUME ["/out", "/cache"]
ENTRYPOINT ["/app/entrypoint.sh", "/opt/venv/bin/python3", "/app/execute_bench.py"]
