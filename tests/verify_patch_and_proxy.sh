#!/usr/bin/env bash
# T1b + T2 verify (no Modal/GPU, just a docker daemon) — the two new sibling-container mechanisms.
#  T1b: commit the agent's UNcommitted /app edits, run the task's UNMODIFIED pre_artifacts.sh inside a
#       detached container, docker cp the patch out -> a non-empty model.patch (capture_patch, Q7).
#  T2 : per-run bridge + a reasoning-proxy container bound 0.0.0.0, reachable by NAME from a sibling,
#       injecting the tier into a chat/completions body and logging it (start_proxies, Q11).
# The full end-to-end run (needs MODAL creds + live GLM endpoint) is still the ticket's own command.
#   ./tests/verify_patch_and_proxy.sh
set -euo pipefail
IMG=${IMG:-glm-bench-t1b-verify}
RUNID=t1b-verify
NET=bench-$RUNID
cd "$(dirname "$0")/.."

echo "==> build $IMG"
docker build -t "$IMG" . >/dev/null

cleanup() {
  docker rm -f "$(docker ps -aq --filter label=bench=$RUNID)" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup
docker network create "$NET" >/dev/null

echo "==> T1b: capture_patch = commit + unmodified pre_artifacts.sh + docker cp -> non-empty patch"
CN=bench-$RUNID-patch
docker run -d --name "$CN" --label bench=$RUNID -w /app --entrypoint sleep "$IMG" infinity >/dev/null
# a clean /app standing in for a task image's repo at base_commit
G="git -c safe.directory=/app -c user.email=a@b -c user.name=a"
docker exec -w /app "$CN" sh -c "rm -rf /app/* /app/.git 2>/dev/null; $G init -q &&
  echo base > file.txt && $G add -A && $G commit -qm base"
BASE=$(docker exec -w /app "$CN" git -c safe.directory=/app rev-parse HEAD)
docker exec -w /app "$CN" sh -c 'echo agent-fix >> /app/file.txt'    # the agent edits WITHOUT committing
TMP=$(mktemp -d)
cat > "$TMP/pre_artifacts.sh" <<EOF
#!/bin/bash
set -uo pipefail
cd /app || exit 0
mkdir -p /logs/artifacts
git config --global --add safe.directory /app 2>/dev/null || true
git diff --binary $BASE HEAD > /logs/artifacts/model.patch 2>/dev/null || true
EOF
# exactly what capture_patch() does: commit under the bench identity, run the script, copy the patch out
docker exec -w /app "$CN" git -c safe.directory=/app -c user.email=bench@local -c user.name=bench add -A
docker exec -w /app "$CN" git -c safe.directory=/app -c user.email=bench@local -c user.name=bench commit -qm bench
docker cp "$TMP/pre_artifacts.sh" "$CN:/tmp/pre_artifacts.sh"
docker exec "$CN" bash /tmp/pre_artifacts.sh
docker cp "$CN:/logs/artifacts/model.patch" "$TMP/model.patch"
test -s "$TMP/model.patch" || { echo "    FAIL: empty model.patch"; exit 1; }
grep -q agent-fix "$TMP/model.patch" || { echo "    FAIL: patch missing the uncommitted agent change"; exit 1; }
echo "    ok: non-empty patch capturing the uncommitted agent change"

echo "==> T2: proxy container bound 0.0.0.0, reachable by name, injects tier + logs it"
docker run -d --name bench-$RUNID-echo --label bench=$RUNID --network "$NET" \
  --entrypoint python3 "$IMG" -c '
import http.server
class H(http.server.BaseHTTPRequestHandler):
  def do_POST(self):
    n=int(self.headers.get("Content-Length",0) or 0); b=self.rfile.read(n)
    self.send_response(200); self.send_header("Content-Type","application/json")
    self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
  def log_message(self,*a): pass
http.server.HTTPServer(("0.0.0.0",9999),H).serve_forever()' >/dev/null
docker run -d --name bench-$RUNID-proxy-off --label bench=$RUNID --network "$NET" \
  -e MODAL_ENDPOINT=http://bench-$RUNID-echo:9999/v1 --entrypoint python3 "$IMG" \
  /app/reasoning_proxy.py --reasoning off --port 8899 >/dev/null
sleep 2
RESP=$(docker run --rm --network "$NET" --entrypoint sh "$IMG" -c \
  "curl -s -X POST http://bench-$RUNID-proxy-off:8899/v1/chat/completions \
     -H 'Content-Type: application/json' -d '{\"messages\":[]}'")
echo "$RESP" | grep -q '"enable_thinking": false' || { echo "    FAIL: tier not injected (resp=$RESP)"; exit 1; }
docker logs bench-$RUNID-proxy-off 2>&1 | grep -q 'injected reasoning tier' || { echo "    FAIL: no injection log"; exit 1; }
echo "    ok: reachable by name, injected enable_thinking:false, logged the injection"

echo "ALL CHECKS PASSED"
