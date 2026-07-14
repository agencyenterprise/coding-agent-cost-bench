#!/usr/bin/env bash
# Ticket T1a verify (plumbing, no Modal/GPU needed) — the sibling-container mechanism the orchestrator
# uses to run an agent: seed the node+CLI bundle into a named volume once (Q6), RO-mount it into a
# fresh sibling on the host daemon (Q2), and invoke the agent as the ABSOLUTE native binary (Q6, they
# are bun-compiled ELFs, not node scripts). Also checks the read-only mount and the label sweep (Q10).
# Requires a running docker daemon.
#   ./tests/verify_dockerized_run.sh
#
# The FULL end-to-end run (needs MODAL_ENDPOINT/KEY/SECRET + a live GLM endpoint, pulls the ~1-2GB
# task image) is the ticket's own command:
#   docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD/results:/out" \
#     -e MODAL_ENDPOINT -e MODAL_KEY -e MODAL_SECRET glm-bench \
#     --task abs-module-cache-flags --models opencode:modal/zai-org/GLM-5.2-FP8 --runs 1
set -euo pipefail
IMG=${IMG:-glm-bench-t1a-verify}
VOL=bench-t1a-verify-agent
cd "$(dirname "$0")/.."

echo "==> build $IMG"
docker build -t "$IMG" . >/dev/null

cleanup() {
  docker rm -f "$(docker ps -aq --filter label=bench=t1a-verify)" >/dev/null 2>&1 || true
  docker volume rm -f "$VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "==> seed bundle volume $VOL from the orchestrator image (setup_bundle_volume)"
docker run --rm --entrypoint sh -v "$VOL:/dst" "$IMG" -c \
  'cp -a /opt/agent/. /dst/ && cp /app/opencode.jsonc /dst/opencode.jsonc'

echo "==> sibling container: RO-mount the bundle, invoke each agent as an absolute native binary"
docker run --rm --name bench-t1a-verify-0 --label bench=t1a-verify \
  --cpus 2 --memory 8192m \
  -v "$VOL:/opt/agent:ro" -w /app \
  -e OPENCODE_CONFIG=/opt/agent/opencode.jsonc -e HOME=/root -e NO_COLOR=1 \
  debian:12-slim sh -c '
    set -e
    /opt/agent/bin/opencode --version >/dev/null || { echo "opencode did not run"; exit 1; }
    /opt/agent/bin/claude --version   >/dev/null || { echo "claude did not run"; exit 1; }
    test -r /opt/agent/opencode.jsonc || { echo "opencode.jsonc not readable"; exit 1; }
    if touch /opt/agent/x 2>/dev/null; then echo "bundle mount is NOT read-only"; exit 1; fi
    echo "    ok: opencode + claude run from the RO bundle, config readable"
  '

echo "==> label sweep removes the run'\''s containers (Q10)"
docker rm -f "$(docker ps -aq --filter label=bench=t1a-verify)" >/dev/null 2>&1 || true
if [ -n "$(docker ps -aq --filter label=bench=t1a-verify)" ]; then
  echo "    FAIL: bench=t1a-verify containers survived the sweep"; exit 1
fi
echo "    ok: no leftover bench-* container"

echo "ALL CHECKS PASSED"
