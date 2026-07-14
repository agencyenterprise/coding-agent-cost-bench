#!/usr/bin/env bash
# Ticket P2 verify — deep-swe is a pinned submodule and the orchestrator discovers exactly 113 tasks,
# each with its instruction.md prompt + task.toml docker_image. Pure discovery: no creds, no daemon.
#   ./tests/verify_vendor_discovery.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-python3}
TASK=${TASK:-abs-module-cache-flags}
PIN=6db64a40f3318d8659238ff34a8cc4b491c49205

echo "==> submodule checked out and pinned to 6db64a4"
test -f deep-swe/tasks/dataset.toml || { echo "FAIL: deep-swe submodule not checked out (git submodule update --init)"; exit 1; }
head=$(git -C deep-swe rev-parse HEAD)
[ "$head" = "$PIN" ] || { echo "FAIL: deep-swe at $head, expected $PIN"; exit 1; }
echo "    ok: $head"

echo "==> glob tasks/*/task.toml discovers exactly 113 (4 dataset-level files skipped)"
n=$($PY execute_bench.py --list 2>/dev/null | wc -l | tr -d ' ')
[ "$n" = "113" ] || { echo "FAIL: discovered $n tasks, expected 113"; exit 1; }
echo "    ok: 113 tasks"

echo "==> --task $TASK resolves and prints its instruction.md + docker_image"
# headers (task_id/docker_image/base_commit/limits/prompt label) go to stderr; prompt body to stdout
hdr=$($PY execute_bench.py --list --task "$TASK" 2>&1 >/dev/null)
grep -q "docker_image : public.ecr.aws/" <<<"$hdr" || { echo "FAIL: no docker_image printed"; exit 1; }
grep -q "prompt (instruction.md, label=instr)" <<<"$hdr" || { echo "FAIL: prompt not labelled instr"; exit 1; }
# the prompt is instruction.md verbatim, emitted on stdout
diff <($PY execute_bench.py --list --task "$TASK" 2>/dev/null) "deep-swe/tasks/$TASK/instruction.md" >/dev/null \
  || { echo "FAIL: printed prompt is not instruction.md verbatim"; exit 1; }
echo "    ok"

echo "==> an unknown --task fails loudly (non-zero)"
if $PY execute_bench.py --list --task definitely-not-a-task >/dev/null 2>&1; then
  echo "FAIL: unknown task did not error"; exit 1
fi
echo "    ok"

echo "ALL CHECKS PASSED"
