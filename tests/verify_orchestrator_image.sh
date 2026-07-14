#!/usr/bin/env bash
# Ticket P1 verify — the orchestrator image can drive the host docker socket as `bench` and carries
# the node+CLI bundle at /opt/agent. Requires a running docker daemon on the host.
#   ./tests/verify_orchestrator_image.sh
set -euo pipefail
IMG=${IMG:-glm-bench-p1-verify}
cd "$(dirname "$0")/.."

echo "==> build $IMG"
docker build -t "$IMG" .

echo "==> bench runs 'docker ps' through the entrypoint (socket group handled, no --group-add)"
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  --entrypoint /app/entrypoint.sh "$IMG" docker ps >/dev/null
echo "    ok"

echo "==> node+CLI bundle present at /opt/agent, invocable via absolute node path"
docker run --rm --entrypoint sh "$IMG" -c '
  set -e
  /opt/agent/bin/node --version | grep -q "^v22" || { echo "bundle node is not v22"; exit 1; }
  for c in opencode claude ccusage; do
    test -e /opt/agent/bin/$c || { echo "missing bundle bin: $c"; exit 1; }
  done
  /opt/agent/bin/node /opt/agent/bin/ccusage --help >/dev/null 2>&1 || true
  echo "    ok: $(/opt/agent/bin/node --version) + opencode + claude + ccusage"
'

echo "==> the SWE-bench parquet bake is gone from the image"
if docker run --rm --entrypoint sh "$IMG" -c 'test -e /home/bench/.cache/huggingface'; then
  echo "    FAIL: huggingface cache present (parquet bake should be gone)"; exit 1
fi
echo "    ok"

echo "ALL CHECKS PASSED"
