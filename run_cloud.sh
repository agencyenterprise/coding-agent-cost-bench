#!/usr/bin/env bash
# Download-and-run: pulls the published cloud image and runs it — no repo checkout, no local
# node/python/opencode/modal installs. Only requirement on this host: Docker + a filled-in .env.
#
#   cp .env.example .env && $EDITOR .env
#   ./run_cloud.sh                          # default: run_auto_endpoint.sh --runs 1 --swe-grade
#   ./run_cloud.sh --runs 1 --swe-grade --jobs 8   # override — replaces the image's default args
#   IMAGE=glm-bench-cloud:local ./run_cloud.sh --no-pull ...   # test a locally-built image
#
# IMAGE defaults to a placeholder — point it at wherever this gets published.
# Always run this via bash (the shebang above) — not sourced/pasted into fish or another shell;
# .env uses bash's `export KEY="val"` syntax.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${IMAGE:-ghcr.io/ae-alignment/glm-bench-cloud:latest}"
command -v docker >/dev/null 2>&1 || { echo "docker not found — install Docker first" >&2; exit 1; }
[ -f .env ] || { echo "need .env with creds — cp .env.example .env and fill it in" >&2; exit 1; }

PULL=1
if [ "${1:-}" = "--no-pull" ]; then PULL=0; shift; fi
[ "$PULL" = 1 ] && docker pull "$IMAGE"
set -a; source .env; set +a
mkdir -p results

docker run --rm \
  -e ANTHROPIC_API_KEY -e GEMINI_API_KEY -e OPENAI_API_KEY \
  -e MODAL_ENDPOINT -e MODAL_KEY -e MODAL_SECRET \
  -e MODAL_TOKEN_ID -e MODAL_TOKEN_SECRET \
  -v "$PWD/results:/app/results" \
  "$IMAGE" "$@"
