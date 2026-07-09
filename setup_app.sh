#!/usr/bin/env bash
# Provision the GLM-5.2 *App* (a hand-rolled SGLang server) on Modal by deploying modal_app.py.
# Counterpart to setup_auto_endpoint.sh — idempotent: reuse the deployed app+URL unless modal_app.py
# OR the hardware tier changed (or --force-deploy).
#
# Prints the OpenAI-compatible /v1 URL on STDOUT (deploy logs go to stderr):
#   ./setup_app.sh --gpu H200 --n-gpus 8            # deploy at a tier + print the URL
#   APP_ENDPOINT="$(./setup_app.sh --gpu H200 --n-gpus 8)"   # what run_app.sh does
# Options: --gpu TYPE (B200) · --n-gpus N (8) · --weights-volume NAME · --model-path PATH · --force-deploy
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; source .env; set +a; }

GPU_TYPE="B200"; N_GPUS="8"; WVOL=""; MPATH=""; FORCE_DEPLOY=""
while [ $# -gt 0 ]; do case "$1" in
  --gpu)            GPU_TYPE="$2"; shift 2;;
  --n-gpus)         N_GPUS="$2"; shift 2;;
  --weights-volume) WVOL="$2"; shift 2;;
  --model-path)     MPATH="$2"; shift 2;;
  --force-deploy)   FORCE_DEPLOY=1; shift;;
  *) shift;;
esac; done

APP_NAME="glm-5-2-app-benchmark"     # must match modal.App(name=...) in modal_app.py
mkdir -p "$PWD/.cache"               # generated state lives under .cache/ (already gitignored)
CACHE="$PWD/.cache/app_endpoint"     # remembered URL, for idempotent reuse
TIER="$PWD/.cache/app_tier.json"     # hardware tier read by modal_app.py at deploy (replaces APP_* env)

# Write the tier ONLY if it changed, so its mtime marks the last real tier change (drives redeploy).
desired="$(printf '{"gpu_type":"%s","n_gpus":%s,"weights_volume":"%s","model_path":"%s"}' \
  "$GPU_TYPE" "$N_GPUS" "$WVOL" "$MPATH")"
[ -f "$TIER" ] && [ "$(cat "$TIER")" = "$desired" ] || printf '%s' "$desired" > "$TIER"

# modal CLI present + authenticated (same preflight as setup_auto_endpoint.sh)
command -v modal >/dev/null 2>&1 || { echo "installing modal CLI..." >&2; pip install -q modal; }
modal app list >/dev/null 2>&1 || { echo "modal not authenticated — running 'modal setup'..." >&2; modal setup; }

# idempotent: reuse the deployed app+URL unless modal_app.py or the tier file changed since that
# deploy (cache mtime = last deploy), or --force-deploy.
if [ -z "$FORCE_DEPLOY" ] && [ -s "$CACHE" ] \
   && [ "$PWD/modal_app.py" -ot "$CACHE" ] && [ "$TIER" -ot "$CACHE" ] \
   && modal app list 2>/dev/null | grep -Ei "$APP_NAME" | grep -qi 'deployed'; then
  url="$(cat "$CACHE")"
  echo ">>> App '$APP_NAME' already deployed (modal_app.py + tier unchanged) — reusing $url" >&2
  echo "    (change --gpu/--n-gpus or edit modal_app.py to auto-redeploy, or --force-deploy)" >&2
  printf '%s\n' "$url"
  exit 0
fi

echo ">>> deploying modal_app.py (SGLang GLM-5.2, ${N_GPUS}x${GPU_TYPE})..." >&2
out="$(modal deploy modal_app.py 2>&1)"; echo "$out" >&2
url="$(printf '%s\n' "$out" | grep -oiE 'https://[a-z0-9._-]*modal\.run[a-z0-9._/-]*' | head -1)"
[ -n "$url" ] || { echo "could not parse the deployed URL from 'modal deploy' output — check it above." >&2; exit 1; }
case "$url" in */v1|*/v1/) : ;; *) url="${url%/}/v1" ;; esac

printf '%s\n' "$url" > "$CACHE"      # cache for idempotent reuse next time
echo ">>> App endpoint: $url" >&2
echo "    (first request cold-starts the GPU; auth uses the same Modal-Key/Modal-Secret)" >&2
printf '%s\n' "$url"                 # STDOUT: the /v1 URL, for capture
