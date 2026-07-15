#!/usr/bin/env bash
# glm-bench entrypoint: start the reasoning-proxy sidecar (router mode) so the GLM high/nothink
# setups can inject their reasoning tier, then run the DeepSWE orchestrator. All args pass through
# to run_deepswe.py (e.g. --setups, --runs, --jobs, --tasks).
#
# Required `docker run` wiring (see README):
#   -p 80:80                                        publish the sidecar so pier's Squid (a host
#                                                   container) can reach it at $HOST_IP:80
#   -v /var/run/docker.sock:/var/run/docker.sock    pier drives the HOST docker daemon
#   -v /work:/work                                  pier job tree at a HOST-ALIGNED path — the host
#                                                   daemon bind-mounts these dirs into task
#                                                   containers, so they must be the same path here
#   -v "$PWD/results:/out"                          report.html + per_run.csv + summary.csv land here
#   -e HOST_IP=<box private ip>                      address Squid uses to reach the sidecar
#   --env-file .env                                 MODAL_ENDPOINT/KEY/SECRET (+ MODAL_TOKEN_* for
#                                                   real billing, ANTHROPIC_API_KEY for opus)
set -euo pipefail

OUT="${OUT_DIR:-/out}"
mkdir -p "$OUT" "${WORK_DIR:-/work}"

if [ -n "${MODAL_ENDPOINT:-}" ]; then
  echo "starting reasoning-proxy sidecar (router) on :80 -> $MODAL_ENDPOINT"
  python3 /app/reasoning_proxy.py --router --port 80 --bind 0.0.0.0 \
    --upstream "$MODAL_ENDPOINT" > "$OUT/proxy.log" 2>&1 &
fi

exec python3 /app/run_deepswe.py "$@"
