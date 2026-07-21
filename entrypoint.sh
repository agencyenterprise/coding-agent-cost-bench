#!/usr/bin/env bash
# glm-bench entrypoint: start the reasoning-proxy sidecar (router mode) so the GLM high/nothink
# setups can inject their reasoning tier, then run the DeepSWE orchestrator. All args pass through
# to run_deepswe.py (e.g. --setups, --runs, --jobs, --tasks).
#
# Required `docker run` wiring (see README):
#   -p 80:80                                        publish the sidecar so pier's Squid (a host
#                                                   container) can reach it at $HOST_IP:80
#   -v /var/run/docker.sock:/var/run/docker.sock    pier drives the HOST docker daemon
#   -v "$DIR:$DIR"  -e OUT_DIR="$DIR"               ONE host-aligned data dir: per-run output,
#                                                   manifest.csv, and the pier job tree all land under
#                                                   $DIR/<timestamp>/ (report generated locally after).
#                                                   Same-path mount is required — the host daemon
#                                                   bind-mounts the job tree into task containers by
#                                                   literal path (docker-out-of-docker).
#   -e HOST_IP=<box private ip>                      address Squid uses to reach the sidecar
#   --env-file .env                                 MODAL_ENDPOINT/KEY/SECRET (+ MODAL_TOKEN_* for
#                                                   real billing, ANTHROPIC_API_KEY for opus)
set -euo pipefail

OUT="${OUT_DIR:-/out}"
mkdir -p "$OUT"     # run_deepswe.py creates the per-run subfolder + its pier-jobs tree under here

if [ -n "${MODAL_ENDPOINT:-}" ]; then
  echo "starting reasoning-proxy sidecar (router) on :80 -> $MODAL_ENDPOINT"
  python3 /app/reasoning_proxy.py --router --port 80 --bind 0.0.0.0 \
    --upstream "$MODAL_ENDPOINT" > "$OUT/proxy.log" 2>&1 &
fi

exec python3 /app/run_deepswe.py "$@"
