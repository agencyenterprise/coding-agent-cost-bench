#!/usr/bin/env bash
# Build the image and run the benchmark + judge fully inside Docker — no host opencode/claude/python.
#
#   ./run_on_docker.sh                 # default matrix, judge with gemini
#   ./run_on_docker.sh --runs 3        # any run_bench.sh flags pass straight through
#   JUDGE=openai ./run_on_docker.sh    # pick the judge model (gemini|openai|anthropic|glm)
#   NO_JUDGE=1 ./run_on_docker.sh      # skip the judge step
#
# Creds come from .env (never baked into the image). results/ is mounted back to the host.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${IMAGE:-glm-bench}"
JUDGE_MODEL="${JUDGE:-gemini}"
command -v docker >/dev/null 2>&1 || { echo "docker not found — install Docker first" >&2; exit 1; }
[ -f .env ] || { echo "need .env with creds — cp .env.example .env and fill it in" >&2; exit 1; }

echo ">>> building $IMAGE (npm/apt layers cached after the first build)"
docker build -t "$IMAGE" .

# our .env is `export FOO="bar"` (which `docker --env-file` can't parse) — source it and forward
# the vars by name (-e VAR passes it from the current env), so quoting/export are handled correctly.
set -a; source .env; set +a
mkdir -p results
dr() {
  docker run --rm \
    -e ANTHROPIC_API_KEY -e GEMINI_API_KEY -e OPENAI_API_KEY \
    -e MODAL_ENDPOINT -e MODAL_KEY -e MODAL_SECRET \
    -v "$PWD/results:/app/results" --entrypoint "$1" "$IMAGE" "${@:2}"
}

echo ">>> benchmark:  run_bench.sh $*"
dr ./run_bench.sh "$@"

if [ -z "${NO_JUDGE:-}" ]; then
  echo ">>> judge:      judge.py --judge $JUDGE_MODEL"
  dr python3 judge.py --judge "$JUDGE_MODEL"
fi

echo ">>> done -> results/report.md  (+ summary.csv, results_detailed.csv, complexity.csv)"
