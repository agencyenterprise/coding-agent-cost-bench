#!/usr/bin/env bash
# Benchmark GLM-5.2 on a hand-rolled Modal APP (custom SGLang server, modal_app.py) —
# the counterpart to run_auto_endpoint.sh (managed AEP). Same harness, same tasks;
# the only variable is AEP vs App. Results -> results/app/.
#
#   ./run_app.sh                      # deploy (setup_app.sh) + bench -> results/app/
#   APP_ENDPOINT=https://…/v1 ./run_app.sh   # skip deploy; bench an App you already have up
#   ./run_app.sh --runs 3             # any bench.sh flag passes through
#   JUDGE=gemini ./run_app.sh         # also build report.md after the run
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; source .env; set +a; }

# Endpoint: APP_ENDPOINT wins (skip deploy); else provision via setup_app.sh, which deploys
# modal_app.py and prints the /v1 URL.
APP_ENDPOINT="${APP_ENDPOINT:-$(./setup_app.sh)}"

# point the harness at the App instead of the AEP (auth stays the same: Modal-Key/Secret proxy tokens)
export MODAL_ENDPOINT="$APP_ENDPOINT"
export RESULTS_DIR="${RESULTS_DIR:-$PWD/results/app}"
mkdir -p "$RESULTS_DIR"
echo ">>> App benchmark  (endpoint: $MODAL_ENDPOINT)  ->  $RESULTS_DIR"
./bench.sh "$@"
[ -n "${JUDGE:-}" ] && RESULTS_DIR="$RESULTS_DIR" python3 judge.py --judge "$JUDGE"
echo ">>> App done -> $RESULTS_DIR/  (compare against results/aep/ for AEP-vs-App)"
