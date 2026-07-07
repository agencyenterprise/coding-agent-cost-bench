#!/usr/bin/env bash
# Success = the kanban app type-checks/builds, its vitest suite passes, and the
# seeded API responds: /api/health -> 200, /api/board -> valid JSON containing
# the deterministic seed (3 columns, 6 cards). Runs in the work dir.
set -e

# Next.js 15/16 needs Node >= 20; fall back to an nvm Node if the host default is older.
node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$node_major" -lt 20 ]; then
  for v in "$HOME"/.nvm/versions/node/v2[0-9]* "$HOME"/.nvm/versions/node/v[3-9][0-9]*; do
    [ -x "$v/bin/node" ] && { export PATH="$v/bin:$PATH"; break; }
  done
fi

[ -f package.json ] || { echo "verify: no package.json — agent produced no app" >&2; exit 1; }
[ -d node_modules ] || npm install

# 1. Build (next build also type-checks under TS strict mode)
npm run build

# 2. Tests — the prompt mandates a vitest suite; missing tests = fail
if grep -q '"vitest"' package.json; then
  npx vitest run
else
  echo "verify: vitest not present in package.json (tests are required)" >&2
  exit 1
fi

# 3. Boot the built app and probe the seeded API
PORT=$((20000 + RANDOM % 20000))
export PORT HOSTNAME=127.0.0.1
npm run start > /tmp/kanban-verify-$PORT.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null; pkill -P "$server_pid" 2>/dev/null; wait "$server_pid" 2>/dev/null || true' EXIT

base="http://127.0.0.1:$PORT"
for i in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$base/api/health" || true)"
  [ "$code" = "200" ] && break
  kill -0 "$server_pid" 2>/dev/null || { echo "verify: server died — see /tmp/kanban-verify-$PORT.log" >&2; tail -30 "/tmp/kanban-verify-$PORT.log" >&2; exit 1; }
  sleep 1
done
[ "$code" = "200" ] || { echo "verify: /api/health never returned 200 (last: $code)" >&2; exit 1; }

# /api/board must return valid JSON with the deterministic seed. The board's
# exact shape is agent-designed, so check structurally: walk the JSON and count
# objects carrying a string title/name — 3 columns + 6 cards => at least 9.
curl -sf "$base/api/board" | node -e '
let s = ""; process.stdin.on("data", d => s += d).on("end", () => {
  let d;
  try { d = JSON.parse(s); } catch { console.error("verify: /api/board is not valid JSON"); process.exit(1); }
  let named = 0;
  (function walk(x) {
    if (Array.isArray(x)) return x.forEach(walk);
    if (x && typeof x === "object") {
      if (typeof x.title === "string" || typeof x.name === "string") named++;
      Object.values(x).forEach(walk);
    }
  })(d);
  if (named < 9) { console.error(`verify: expected >=9 titled objects (3 columns + 6 cards), found ${named}`); process.exit(1); }
  console.log(`verify: board OK (${named} titled objects)`);
});
'

echo "verify: all checks passed"
