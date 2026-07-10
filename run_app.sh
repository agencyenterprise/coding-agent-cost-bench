#!/usr/bin/env bash
# Benchmark GLM-5.2 on a hand-rolled Modal APP (custom SGLang server, modal_app.py) — the
# counterpart to run_auto_endpoint.sh (managed AEP). Same harness/tasks; the variables are AEP vs
# App and, via the tier flags, the hardware.
#
#   ./run_app.sh                                    # deploy 8×B200 (default) + bench
#   ./run_app.sh --tier 8xH200 --runs 3             # a named tier (gpu+count+rate baked in)
#   ./run_app.sh --gpu B200 --n-gpus 4 --rate 25.3  # explicit tier
#   ./run_app.sh --app-endpoint https://…/v1        # skip deploy; bench an App already up
#   ./run_app.sh --tier 8xH200 --judge gemini       # also build report.md after
#
# Flags (everything else passes through to bench.sh, e.g. --task / --prompts):
#   --tier NAME        8xB200 | 8xH200       (sets gpu+count+rate; explicit flags override)
#   --gpu TYPE         GPU type              (default B200)
#   --n-gpus N         GPU count / TP size   (default 8)
#   --rate USD_PER_HR  $/hr for this tier's cost calc  (default 50.7)
#   --results-dir DIR  results location      (default results/app-<N>x<GPU>-<timestamp>)
#   --app-endpoint URL bench an already-deployed App; skips setup_app.sh
#   --force-deploy     redeploy even if unchanged
#   --judge M          build report.md with judge M (gemini|openai|anthropic|glm) after the run
#   --swe-grade        after the run, grade SWE tasks on Modal (official Docker) -> resolved.json
# (Secrets — MODAL_*, HF_TOKEN — still come from .env; only knobs are flags.)
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; source .env; set +a; }

GPU=""; NGPUS=""; RATE=""; RDIR=""; APP_ENDPOINT=""; FORCE=""; JUDGE=""; TIER=""; SWE_GRADE=""
_pass=()
while [ $# -gt 0 ]; do
  case "$1" in
    --tier)         TIER="$2"; shift 2;;
    --gpu)          GPU="$2"; shift 2;;
    --n-gpus)       NGPUS="$2"; shift 2;;
    --rate)         RATE="$2"; shift 2;;
    --results-dir)  RDIR="$2"; shift 2;;
    --app-endpoint) APP_ENDPOINT="$2"; shift 2;;
    --force-deploy) FORCE=1; shift;;
    --judge)        JUDGE="$2"; shift 2;;
    --swe-grade)    SWE_GRADE=1; shift;;
    -h|--help)      sed -n '2,21p' "$0"; exit 0;;
    *)              _pass+=("$1"); shift;;
  esac
done
set -- ${_pass[@]+"${_pass[@]}"}

# named tiers (verified list rates); explicit --gpu/--n-gpus/--rate override
case "$TIER" in
  8xB200) GPU="${GPU:-B200}"; NGPUS="${NGPUS:-8}"; RATE="${RATE:-50.7}";;
  8xH200) GPU="${GPU:-H200}"; NGPUS="${NGPUS:-8}"; RATE="${RATE:-36.6}";;
  "")     ;;
  *)      echo "unknown --tier '$TIER' (8xB200|8xH200)" >&2; exit 1;;
esac
GPU="${GPU:-B200}"; NGPUS="${NGPUS:-8}"; RATE="${RATE:-50.7}"
RDIR="${RDIR:-$PWD/results/app-${NGPUS}x${GPU}-$(date +%Y-%m-%dT%H%M%S)}"

# provision the App at this tier (setup_app.sh writes .app_tier.json + deploys), unless an endpoint
# was given. MODAL_ENDPOINT is an allowed .env var — the harness reads it to reach the endpoint.
if [ -z "$APP_ENDPOINT" ]; then
  APP_ENDPOINT="$(./setup_app.sh --gpu "$GPU" --n-gpus "$NGPUS" ${FORCE:+--force-deploy})"
fi
export MODAL_ENDPOINT="$APP_ENDPOINT"

mkdir -p "$RDIR"
echo ">>> App benchmark  (${NGPUS}x${GPU} @ \$${RATE}/hr, endpoint: $MODAL_ENDPOINT)  ->  $RDIR"
./bench.sh --results-dir "$RDIR" --rate "$RATE" "$@"
[ -n "$JUDGE" ] && python3 judge.py --judge "$JUDGE" --results-dir "$RDIR" --rate "$RATE"
[ -n "$SWE_GRADE" ] && ./grade_swe.sh --results-dir "$RDIR" --billing-app glm-5-2-app-benchmark
echo ">>> App done -> $RDIR/  (compare tiers by their cost_per_successful_task)"
