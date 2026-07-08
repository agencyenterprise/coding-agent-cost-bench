#!/usr/bin/env bash
# Benchmark GLM-5.2 on Modal's managed AUTO-ENDPOINT (AEP).
#
# Ensures the AEP is up (setup_auto_endpoint.sh, idempotent), points the harness at it via MODAL_ENDPOINT,
# and runs the standard bench into results/aep/. Pair with run_app.sh (same harness,
# a hand-rolled App) for an apples-to-apples AEP-vs-App comparison — the only variable is the
# hosting mode.
#
#   ./run_auto_endpoint.sh                 # default matrix -> results/aep/
#   ./run_auto_endpoint.sh --runs 3        # any bench.sh flag passes through
#   SKIP_SETUP=1 ./run_auto_endpoint.sh    # assume the AEP is already up
#   JUDGE=gemini ./run_auto_endpoint.sh    # also build report.md after the run
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; source .env; set +a; }

: "${MODAL_ENDPOINT:?set MODAL_ENDPOINT in .env (the AEP /v1 URL), or run ./setup_auto_endpoint.sh first}"

# bring the AEP up if needed (idempotent; skip if you know it's warm)
[ -n "${SKIP_SETUP:-}" ] || ./setup_auto_endpoint.sh

export RESULTS_DIR="${RESULTS_DIR:-$PWD/results/aep}"
mkdir -p "$RESULTS_DIR"
echo ">>> AEP benchmark  (endpoint: $MODAL_ENDPOINT)  ->  $RESULTS_DIR"
./bench.sh "$@"
[ -n "${JUDGE:-}" ] && RESULTS_DIR="$RESULTS_DIR" python3 judge.py --judge "$JUDGE"
echo ">>> AEP done -> $RESULTS_DIR/  (summary.csv; run 'RESULTS_DIR=$RESULTS_DIR python3 judge.py --judge <m>' for report.md)"
