#!/usr/bin/env bash
# Benchmark GLM-5.2 on Modal's managed AUTO-ENDPOINT (AEP).
#
# Ensures the AEP is up (setup_auto_endpoint.sh, idempotent), points the harness at it via
# MODAL_ENDPOINT, and runs the standard bench into results/aep/. Pair with run_app.sh (same harness,
# a hand-rolled App) for an apples-to-apples AEP-vs-App comparison.
#
#   ./run_auto_endpoint.sh                 # default matrix -> results/aep/
#   ./run_auto_endpoint.sh --runs 3        # any bench.sh flag passes through
#   ./run_auto_endpoint.sh --skip-setup    # assume the AEP is already up
#   ./run_auto_endpoint.sh --judge gemini  # also build report.md after the run
#
# Flags (rest passes to bench.sh):
#   --rate USD_PER_HR  $/hr for the cost calc     (default 50.7 — the AEP's 8×B200)
#   --results-dir DIR  results location           (default results/aep)
#   --judge M          build report.md with judge M after the run
#   --skip-setup       don't run setup_auto_endpoint.sh (assume warm)
# (MODAL_* creds come from .env.)
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; source .env; set +a; }

RATE="50.7"; RDIR="$PWD/results/aep"; JUDGE=""; SKIP_SETUP=""
_pass=()
while [ $# -gt 0 ]; do
  case "$1" in
    --rate)        RATE="$2"; shift 2;;
    --results-dir) RDIR="$2"; shift 2;;
    --judge)       JUDGE="$2"; shift 2;;
    --skip-setup)  SKIP_SETUP=1; shift;;
    -h|--help)     sed -n '2,18p' "$0"; exit 0;;
    *)             _pass+=("$1"); shift;;
  esac
done
set -- ${_pass[@]+"${_pass[@]}"}

: "${MODAL_ENDPOINT:?set MODAL_ENDPOINT in .env (the AEP /v1 URL), or run ./setup_auto_endpoint.sh first}"

[ -n "$SKIP_SETUP" ] || ./setup_auto_endpoint.sh

mkdir -p "$RDIR"
echo ">>> AEP benchmark  (endpoint: $MODAL_ENDPOINT @ \$${RATE}/hr)  ->  $RDIR"
./bench.sh --results-dir "$RDIR" --rate "$RATE" "$@"
[ -n "$JUDGE" ] && python3 judge.py --judge "$JUDGE" --results-dir "$RDIR" --rate "$RATE"
echo ">>> AEP done -> $RDIR/  (run 'python3 judge.py --judge <m> --results-dir $RDIR' for report.md)"
