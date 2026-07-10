#!/usr/bin/env bash
# Grade the SWE-bench tasks of a finished bench run with the OFFICIAL grader, on Modal (x86).
#
# Two-phase: bench.sh already generated the agents' patches (results/<dir>/*/final_repo). This
# harvests them (make_predictions.py) then evaluates each on Modal using the instance's own
# prebuilt SWE-bench Docker image (swe_eval_modal.py) — no docker-in-docker, right Python per repo.
# Writes <results-dir>/resolved.json, then re-runs aggregate.py so the report's SWE pass/fail comes
# from the cloud grade instead of the fragile host verify.sh.
#
#   ./grade_swe.sh --results-dir results/aep
#   ./grade_swe.sh --results-dir results/app-8xH200 --status-host pass   # extra args pass to swe_eval_modal.py
#
# Needs: Docker not required locally; a Python venv with swebench+pyarrow+modal (bootstrapped here,
# cached under .cache/), and `modal setup` done once for auth. The SWE-bench_Verified parquet must be
# in the HF cache (make_swebench_task.py populates it).
set -euo pipefail
cd "$(dirname "$0")"

RDIR=""; BILLING_APP="ep-Modal-Auto-Endpoints"; PASS=()
while [ $# -gt 0 ]; do case "$1" in
  --results-dir) RDIR="$2"; shift 2;;
  --billing-app) BILLING_APP="$2"; shift 2;;   # 'description' to sum for the actual bill (App: glm-5-2-app-benchmark)
  -h|--help) sed -n '2,18p' "$0"; exit 0;;
  *) PASS+=("$1"); shift;;
esac; done
[ -n "$RDIR" ] || { echo "need --results-dir DIR (e.g. results/aep)" >&2; exit 1; }
[ -f "$RDIR/manifest.csv" ] || { echo "no manifest in $RDIR — run the bench first" >&2; exit 1; }

# tooling venv (swebench + pyarrow + modal); cached under .cache/ (gitignored), reused across runs
VENV="$PWD/.cache/swe-venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo ">>> creating SWE grading venv (swebench + pyarrow + modal) — one-time..." >&2
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q swebench pyarrow modal
fi
PY="$VENV/bin/python"

# fail early with a clear message if Modal isn't authed (else swe_eval_modal dies mid-run)
"$PY" -c 'import modal' 2>/dev/null || { echo "modal not importable in $VENV" >&2; exit 1; }
[ -f "$HOME/.modal.toml" ] || { echo "Modal not authenticated — run 'modal setup' first." >&2; exit 1; }

echo ">>> [1/4] harvest agent patches from $RDIR"
"$PY" make_predictions.py --results-dir "$RDIR"

echo ">>> [2/4] grade on Modal (x86 per-instance images)"
"$PY" swe_eval_modal.py --predictions "$RDIR/predictions.jsonl" ${PASS[@]+"${PASS[@]}"}

echo ">>> [3/4] pull the actual endpoint bill for the run window ($BILLING_APP)"
"$PY" billing.py --results-dir "$RDIR" --app "$BILLING_APP" \
  || echo "    (billing pull failed — report will just skip the ground-truth line)" >&2

echo ">>> [4/4] re-aggregate with the official grades"
"$PY" aggregate.py --results-dir "$RDIR" --no-open

echo ">>> done — SWE pass/fail in $RDIR/report.html now comes from the Modal Docker grader"
