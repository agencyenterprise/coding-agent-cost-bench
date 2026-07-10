#!/usr/bin/env bash
# Build the image and run the benchmark + judge fully inside Docker — no host opencode/claude/python.
#
#   ./run_on_docker.sh                 # build, run the default matrix, judge with gemini
#   ./run_on_docker.sh --runs 3        # any bench.sh flags pass straight through
#   ./run_on_docker.sh --judge openai  # pick the judge model (gemini|openai|anthropic|glm)
#   ./run_on_docker.sh --judge-only    # re-judge existing results/ (no bench -> no GPU cost)
#   ./run_on_docker.sh --bench-only    # run the bench, skip the judge
#   ./run_on_docker.sh --no-build ...  # reuse the current image (skip docker build)
#   ./run_on_docker.sh --jobs 2 ...    # in-container concurrency (default 3)
#
# Creds come from .env (sourced + forwarded by name; never baked in). results/ is mounted to the host.
# Default results dir is results/docker-<timestamp>/ (or pass --results-dir). --judge-only defaults to
# results/ unless --results-dir is set.
# Defaults to --jobs 3 in-container: 5 simultaneous agents OOM the shared Docker VM. Also free VM
# headroom (stop unused containers) or raise Docker Desktop memory if you still see "Killed".
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${IMAGE:-glm-bench}"
JUDGE_MODEL="gemini"
RDIR=""
command -v docker >/dev/null 2>&1 || { echo "docker not found — install Docker first" >&2; exit 1; }
[ -f .env ] || { echo "need .env with creds — cp .env.example .env and fill it in" >&2; exit 1; }

BUILD=1; DO_BENCH=1; DO_JUDGE=1
pass=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build)   BUILD=0; shift ;;
    --judge-only) DO_BENCH=0; shift ;;
    --bench-only) DO_JUDGE=0; shift ;;
    --judge)      JUDGE_MODEL="$2"; shift 2 ;;
    --results-dir) RDIR="$2"; shift 2 ;;
    *)            pass+=("$1"); shift ;;
  esac
done
# 5 simultaneous Node agents + pip installs OOM the shared Docker VM (the last-launched task gets
# SIGKILLed). Default to gentler in-container concurrency; override by passing your own --jobs/-j.
case " ${pass[*]:-} " in
  *" --jobs "* | *" -j "*) : ;;
  *) pass+=(--jobs "${DOCKER_JOBS:-3}") ;;
esac
set -- ${pass[@]+"${pass[@]}"}   # remaining args pass through to bench.sh

# build if requested, or if the image doesn't exist yet
if [ "$BUILD" = 1 ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo ">>> building $IMAGE (npm/apt layers cached after the first build)"
  docker build -t "$IMAGE" .
fi

# our .env is `export FOO="bar"` (docker --env-file can't parse that) — source it and forward by name
set -a; source .env; set +a
if [ -z "$RDIR" ]; then
  if [ "$DO_BENCH" = 1 ]; then
    RDIR_REL="docker-$(date +%Y-%m-%dT%H%M%S)"
  else
    RDIR_REL="."   # judge-only: reuse results/ unless --results-dir given
  fi
else
  case "$RDIR" in
    "$PWD/results"/*) RDIR_REL="${RDIR#"$PWD/results/"}" ;;
    results/*)        RDIR_REL="${RDIR#results/}" ;;
    /*)               RDIR_REL="${RDIR##*/}" ;;
    *)                RDIR_REL="$RDIR" ;;
  esac
fi
HOST_RDIR="$PWD/results/$RDIR_REL"
mkdir -p "$HOST_RDIR"
_bench_rdir="/app/results/$RDIR_REL"
dr() {
  docker run --rm \
    -e ANTHROPIC_API_KEY -e GEMINI_API_KEY -e OPENAI_API_KEY \
    -e MODAL_ENDPOINT -e MODAL_KEY -e MODAL_SECRET \
    -v "$PWD/results:/app/results" --entrypoint "$1" "$IMAGE" "${@:2}"
}
[ "$DO_BENCH" = 1 ] && { echo ">>> benchmark:  bench.sh --results-dir $_bench_rdir $*"; dr ./bench.sh --results-dir "$_bench_rdir" "$@"; }
[ "$DO_JUDGE" = 1 ] && { echo ">>> judge:      judge.py --judge $JUDGE_MODEL --results-dir $_bench_rdir"; dr python3 judge.py --judge "$JUDGE_MODEL" --results-dir "$_bench_rdir"; }
echo ">>> done -> $HOST_RDIR/  (+ summary.csv, results_detailed.csv, complexity.csv, report.md)"
