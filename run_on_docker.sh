#!/usr/bin/env bash
# Build the image and run the benchmark + judge fully inside Docker — no host opencode/claude/python.
#
#   ./run_on_docker.sh                 # build, run the default matrix, judge with gemini
#   ./run_on_docker.sh --runs 3        # any run_bench.sh flags pass straight through
#   JUDGE=openai ./run_on_docker.sh    # pick the judge model (gemini|openai|anthropic|glm)
#   ./run_on_docker.sh --judge-only    # re-judge existing results/ (no bench -> no GPU cost)
#   ./run_on_docker.sh --bench-only    # run the bench, skip the judge
#   ./run_on_docker.sh --no-build ...  # reuse the current image (skip docker build)
#   DOCKER_JOBS=2 ./run_on_docker.sh   # in-container concurrency (default 3; your own --jobs wins)
#
# Creds come from .env (sourced + forwarded by name; never baked in). results/ is mounted to the host.
# Defaults to --jobs 3 in-container: 5 simultaneous agents OOM the shared Docker VM. Also free VM
# headroom (stop unused containers) or raise Docker Desktop memory if you still see "Killed".
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${IMAGE:-glm-bench}"
JUDGE_MODEL="${JUDGE:-gemini}"
command -v docker >/dev/null 2>&1 || { echo "docker not found — install Docker first" >&2; exit 1; }
[ -f .env ] || { echo "need .env with creds — cp .env.example .env and fill it in" >&2; exit 1; }

BUILD=1; DO_BENCH=1; DO_JUDGE=1
[ -n "${NO_JUDGE:-}" ] && DO_JUDGE=0
pass=()
for a in "$@"; do
  case "$a" in
    --no-build)   BUILD=0 ;;
    --judge-only) DO_BENCH=0 ;;
    --bench-only) DO_JUDGE=0 ;;
    *)            pass+=("$a") ;;
  esac
done
# 5 simultaneous Node agents + pip installs OOM the shared Docker VM (the last-launched task gets
# SIGKILLed). Default to gentler in-container concurrency; override by passing your own --jobs/-j.
case " ${pass[*]:-} " in
  *" --jobs "* | *" -j "*) : ;;
  *) pass+=(--jobs "${DOCKER_JOBS:-3}") ;;
esac
set -- ${pass[@]+"${pass[@]}"}   # remaining args pass through to run_bench.sh

# build if requested, or if the image doesn't exist yet
if [ "$BUILD" = 1 ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo ">>> building $IMAGE (npm/apt layers cached after the first build)"
  docker build -t "$IMAGE" .
fi

# our .env is `export FOO="bar"` (docker --env-file can't parse that) — source it and forward by name
set -a; source .env; set +a
mkdir -p results
dr() {
  docker run --rm \
    -e ANTHROPIC_API_KEY -e GEMINI_API_KEY -e OPENAI_API_KEY \
    -e MODAL_ENDPOINT -e MODAL_KEY -e MODAL_SECRET \
    -v "$PWD/results:/app/results" --entrypoint "$1" "$IMAGE" "${@:2}"
}

[ "$DO_BENCH" = 1 ] && { echo ">>> benchmark:  run_bench.sh $*"; dr ./run_bench.sh "$@"; }
[ "$DO_JUDGE" = 1 ] && { echo ">>> judge:      judge.py --judge $JUDGE_MODEL"; dr python3 judge.py --judge "$JUDGE_MODEL"; }
echo ">>> done -> results/report.md  (+ summary.csv, results_detailed.csv, complexity.csv)"
