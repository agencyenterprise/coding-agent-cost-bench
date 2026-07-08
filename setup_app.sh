#!/usr/bin/env bash
# Provision the GLM-5.2 *App* (a hand-rolled SGLang server) on Modal by deploying modal_app.py.
# Counterpart to setup_auto_endpoint.sh (the managed Auto-Endpoint) — idempotent the same way:
# if the app is already deployed and we know its URL, reuse it instead of re-deploying.
#
# Prints the OpenAI-compatible /v1 URL on STDOUT (deploy logs go to stderr), so callers capture it:
#   APP_ENDPOINT="$(./setup_app.sh)"          # what run_app.sh does
#   ./setup_app.sh                            # standalone: (deploy if needed +) print the URL
#   FORCE_DEPLOY=1 ./setup_app.sh             # always re-deploy (e.g. after changing APP_N_GPUS / args)
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; source .env; set +a; }

APP_NAME="glm-5-2-app-benchmark"     # must match modal.App(name=...) in modal_app.py
CACHE="$PWD/.app_endpoint"           # remembered URL, for idempotent reuse

# modal CLI present + authenticated (same preflight as setup_auto_endpoint.sh)
command -v modal >/dev/null 2>&1 || { echo "installing modal CLI..." >&2; pip install -q modal; }
modal app list >/dev/null 2>&1 || { echo "modal not authenticated — running 'modal setup'..." >&2; modal setup; }

# idempotent: reuse the deployed app+URL, but ONLY if modal_app.py hasn't changed since that deploy
# (the cache file's mtime marks the last deploy). Edit modal_app.py -> auto-redeploy; no FORCE_DEPLOY
# needed. (Changing APP_GPU_TYPE/APP_N_GPUS without editing the file still needs FORCE_DEPLOY=1.)
if [ -z "${FORCE_DEPLOY:-}" ] && [ -s "$CACHE" ] && [ "$PWD/modal_app.py" -ot "$CACHE" ] \
   && modal app list 2>/dev/null | grep -Ei "$APP_NAME" | grep -qi 'deployed'; then
  url="$(cat "$CACHE")"
  echo ">>> App '$APP_NAME' already deployed (modal_app.py unchanged) — reusing $url" >&2
  echo "    (edit modal_app.py to auto-redeploy, or FORCE_DEPLOY=1 to force)" >&2
  printf '%s\n' "$url"
  exit 0
fi

echo ">>> deploying modal_app.py (custom SGLang GLM-5.2 server)..." >&2
out="$(modal deploy modal_app.py 2>&1)"; echo "$out" >&2
url="$(printf '%s\n' "$out" | grep -oiE 'https://[a-z0-9._-]*modal\.run[a-z0-9._/-]*' | head -1)"
[ -n "$url" ] || { echo "could not parse the deployed URL from 'modal deploy' output — check it above." >&2; exit 1; }
case "$url" in */v1|*/v1/) : ;; *) url="${url%/}/v1" ;; esac

printf '%s\n' "$url" > "$CACHE"      # cache for idempotent reuse next time
echo ">>> App endpoint: $url" >&2
echo "    (first request cold-starts the GPU; auth uses the same Modal-Key/Modal-Secret)" >&2
printf '%s\n' "$url"                 # STDOUT: the /v1 URL, for capture
