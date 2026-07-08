# SPIKE — reproducible benchmark env: opencode + Claude Code + python, no host deps.
# Kills the "host environment" class of problems (e.g. Python 3.14 breaking old repos, local CLI drift).
#
#   docker build -t glm-bench .
#   docker run --rm --env-file .env -v "$PWD/results:/app/results" glm-bench --runs 1
#
# Creds come at RUNTIME (never baked): ANTHROPIC_API_KEY, MODAL_ENDPOINT/KEY/SECRET, GEMINI_API_KEY.
# The GLM endpoint stays on Modal (external) — the container just needs the URL + keys + outbound net.

FROM node:22-bookworm-slim

# python 3.11 (bookworm) is a broad-compat sweet spot for the task repos (has pkgutil.get_loader,
# works with pytest 4–8) — deliberately NOT the host's 3.14 that broke old libs; git for clones;
# coreutils gives a real `timeout`; sqlite3 for the ccusage DB checkpoint.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git curl ca-certificates coreutils sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# pinned agent CLIs
RUN npm i -g opencode-ai@1.17.13 @anthropic-ai/claude-code@2.1.193

# LiteLLM proxy for the `deepclaude` harness: Claude Code speaks the Anthropic Messages API
# (POST /v1/messages), but the Modal GLM-5.2 endpoint is OpenAI-compatible (/v1/chat/completions)
# and auth'd by Modal-Key/Modal-Secret headers. LiteLLM translates between the two and injects the
# headers. NOTE: needs a version whose /v1/messages endpoint routes non-Anthropic providers through
# the messages->completions adapter — 1.63.x only supported anthropic/bedrock/vertex and 500s on an
# openai/ backend ("Anthropic messages provider config not found"). 1.77.3 has the adapter fallback.
RUN pip3 install --no-cache-dir --break-system-packages 'litellm[proxy]==1.77.3'

# non-root user — claude --dangerously-skip-permissions refuses to run as root
RUN useradd -m -u 1001 bench
USER bench

# skip Claude Code's first-run onboarding so `claude -p` is non-interactive (RISK: verify the key/flag)
RUN printf '{"hasCompletedOnboarding":true}\n' > /home/bench/.claude.json

WORKDIR /app
USER root
COPY . /app
RUN chmod +x run_bench.sh clear_results.sh 2>/dev/null || true && \
    chown -R bench:bench /app

USER bench
ENV OPENCODE_CONFIG=/app/opencode.jsonc NO_COLOR=1
RUN npx -y ccusage --help >/dev/null 2>&1 || true   # prefetch ccusage so per-run usage.json is clean

ENTRYPOINT ["./run_bench.sh"]
