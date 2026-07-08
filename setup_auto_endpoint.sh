#!/usr/bin/env bash
# Idempotent setup of the GLM-5.2 Modal auto-endpoint + its proxy auth token.
# Re-runnable: creates the endpoint only if missing, and enforces exactly ONE proxy token
# that matches .env. Reuses the pre-downloaded weights volume (no re-download).
#
#   ./setup_auto_endpoint.sh [options]
#
# Env vars are accepted as fallbacks (MODAL_ENDPOINT_NAME, GLM_MODEL, GLM_VOLUME, ...).
set -euo pipefail

NAME="${MODAL_ENDPOINT_NAME:-Modal-Auto-Endpoints}"
MODEL="${GLM_MODEL:-zai-org/GLM-5.2-FP8}"
VOLUME="${GLM_VOLUME:-glm-5-2-weights}"
VOLUME_PATH="${GLM_VOLUME_PATH:-zai-org/GLM-5.2-FP8}"
ENV_NAME="${MODAL_ENV:-main}"
ENV_FILE=".env"
WAIT_TRIES="${WAIT_TRIES:-160}"
WAIT_SLEEP="${WAIT_SLEEP:-15}"

usage() {
  cat >&2 <<EOF
Usage: ./setup_auto_endpoint.sh [options]
      --name NAME           Modal auto-endpoint name     [$NAME]
      --model ID            model id on the endpoint     [$MODEL]
      --volume NAME         pre-downloaded weights vol   [$VOLUME]
      --volume-path PATH    mount path inside container  [$VOLUME_PATH]
      --env NAME            proxy-token allowed env      [$ENV_NAME]
      --env-file PATH       file with MODAL_KEY/SECRET   [$ENV_FILE]
      --wait-tries N        provisioning poll attempts   [$WAIT_TRIES]
      --wait-sleep SECS     seconds between polls        [$WAIT_SLEEP]
  -h, --help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --name)        NAME="$2";        shift 2;;
    --model)       MODEL="$2";       shift 2;;
    --volume)      VOLUME="$2";      shift 2;;
    --volume-path) VOLUME_PATH="$2"; shift 2;;
    --env)         ENV_NAME="$2";    shift 2;;
    --env-file)    ENV_FILE="$2";    shift 2;;
    --wait-tries)  WAIT_TRIES="$2";  shift 2;;
    --wait-sleep)  WAIT_SLEEP="$2";  shift 2;;
    -h|--help)     usage; exit 0;;
    *) echo "unknown arg: $1" >&2; usage; exit 1;;
  esac
done

# 1. modal CLI present + authenticated
command -v modal >/dev/null 2>&1 || { echo "installing modal CLI..."; pip install -q modal; }
modal app list >/dev/null 2>&1 || { echo "modal not authenticated — running 'modal setup'..."; modal setup; }

# 2. proxy auth token — enforce exactly ONE, matching .env
#    (server empty -> create; matches -> allow; mismatch/missing -> warn + stop)
env_key="$(grep -E '^[[:space:]]*export[[:space:]]+MODAL_KEY=' "$ENV_FILE" 2>/dev/null \
            | head -1 | sed -E 's/^[^=]*=//; s/^["'\'']//; s/["'\'']$//')"
server_ids="$(modal workspace proxy-tokens list 2>/dev/null | grep -oE 'wk-[A-Za-z0-9]+' | sort -u)"
n="$(printf '%s\n' "$server_ids" | grep -c . || true)"

if [ "$n" -eq 0 ]; then
  echo "no proxy token on the server — creating one:"
  echo
  modal workspace proxy-tokens create
  echo
  echo ">>> copy the values above into $ENV_FILE, then re-run ./setup_auto_endpoint.sh :"
  echo '    export MODAL_KEY="wk-..."'
  echo '    export MODAL_SECRET="ws-..."'
  exit 1
elif [ "$n" -gt 1 ]; then
  echo "WARNING: $n proxy tokens on the server (want exactly 1):" >&2
  printf '  %s\n' $server_ids >&2
  echo "delete the extras (modal workspace proxy-tokens delete <id>) and re-run." >&2
  exit 1
fi

# exactly one token on the server
if [ -z "$env_key" ]; then
  echo "server has a proxy token ($server_ids) but MODAL_KEY is empty in $ENV_FILE." >&2
  echo "get the secret from whoever created it, set it in $ENV_FILE, then re-run:" >&2
  echo "    export MODAL_KEY=\"$server_ids\"  ;  export MODAL_SECRET=\"ws-...\"" >&2
  exit 1
elif [ "$env_key" = "$server_ids" ]; then
  echo "MODAL_KEY matches the server token ($server_ids) — allowing env '$ENV_NAME'..."
  modal workspace proxy-tokens allow "$server_ids" "$ENV_NAME" 2>/dev/null \
    || echo "  (allow skipped/failed — fine if RBAC is off)"
else
  echo "MISMATCH: $ENV_FILE has '$env_key' but the server token is '$server_ids'." >&2
  echo "use the value that token was created with, or delete it and recreate:" >&2
  echo "    modal workspace proxy-tokens delete $server_ids && ./setup_auto_endpoint.sh" >&2
  exit 1
fi

# 3. endpoint — create only if missing (idempotent), reusing the weights volume
if modal endpoint list --json 2>/dev/null | grep -qi "$NAME"; then
  echo "auto-endpoint matching '$NAME' already exists — skipping create."
else
  echo "creating auto-endpoint '$NAME' for $MODEL (reusing volume '$VOLUME')..."
  modal endpoint create \
    --name "$NAME" \
    --model "$MODEL" \
    --custom-volume-name "$VOLUME" \
    --custom-volume-path "$VOLUME_PATH"
fi

# 4. wait until the endpoint leaves 'provisioning' (endpoint list --json has no URL — only status)
echo
echo "waiting for '$NAME' to finish provisioning (8×B200 cold start — a few minutes)..."
tmp="$(mktemp)"; status=""
set +e   # the poll must never kill the script
for i in $(seq 1 "$WAIT_TRIES"); do
  modal endpoint list --json > "$tmp" 2>/dev/null
  status="$(python3 - "$NAME" "$tmp" <<'PY'
import json, sys
name = sys.argv[1].lower()
try:
    data = json.load(open(sys.argv[2]))
except Exception:
    print(""); sys.exit(0)
for it in (data if isinstance(data, list) else []):
    if isinstance(it, dict) and name in str(it.get("name", "")).lower():
        print(str(it.get("status") or "").lower()); sys.exit(0)
print("")
PY
)"
  case "$status" in
    provisioning|pending|creating|starting|building|initializing|queued|"")
      printf '  [%4ds] status=%s   \r' "$((i * WAIT_SLEEP))" "${status:-?}" >&2
      sleep "$WAIT_SLEEP" ;;
    failed|error|errored|stopped|terminated)
      set -e; rm -f "$tmp"; echo >&2
      echo "endpoint '$NAME' is '$status' — check the dashboard." >&2; exit 1 ;;
    *) break ;;   # ready-ish (running / ready / deployed / ...)
  esac
done
set -e
rm -f "$tmp"
echo >&2

case "${status:-}" in
  ""|provisioning|pending|creating|starting|building|initializing|queued)
    echo "still provisioning after $((WAIT_TRIES * WAIT_SLEEP))s — re-run ./setup_auto_endpoint.sh later." >&2
    exit 1 ;;
esac
echo "endpoint '$NAME' is up (status=$status)."

# The auto-endpoint URL is NOT in `endpoint list` — it's derived from workspace + name, so
# it's the same URL as before (name unchanged). Confirm MODAL_ENDPOINT is set.
cur="$(grep -E '^[[:space:]]*export[[:space:]]+MODAL_ENDPOINT=' "$ENV_FILE" 2>/dev/null | head -1 | sed -E 's/^[^=]*=//; s/^["'\'']//; s/["'\'']$//')"
if [ -n "$cur" ]; then
  echo "MODAL_ENDPOINT is set: $cur"
  echo "verify it serves the new endpoint (first call cold-starts the 8×B200):"
  echo "  source .env && curl -H \"Modal-Key: \$MODAL_KEY\" -H \"Modal-Secret: \$MODAL_SECRET\" $cur/models"
else
  echo "set MODAL_ENDPOINT in $ENV_FILE — copy the URL from the dashboard:"
  echo "  https://modal.com/endpoints/ (open '$NAME') → use <url>/v1"
fi
echo "then: source .env && ./bench.sh"
