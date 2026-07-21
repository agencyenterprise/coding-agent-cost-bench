#!/usr/bin/env bash
# create_study.sh — name a benchmark run as a study, then freeze it into a committable result.
#
#   ./create_study.sh <name> [run-dir] [report-args...]
#
#   1. Names the study:  the raw run becomes  study/<name>/  (LOCAL scratch — study/ is gitignored).
#        - run-dir defaults to study/<name> if it already exists, else the newest dir under study/.
#        - if study/<name> doesn't exist yet, the chosen run is renamed (mv) to study/<name>.
#   2. Generates the report on it  (benchmark_progress_report.py -> CSVs + billing.json + report).
#   3. Freezes the LIGHT artifacts into  results/<name>/  — this is what you commit:
#        manifest.csv, per_run.csv, summary.csv, deepswe_task_difficulty.csv, billing.json,
#        progress_report.html, tasks.txt (task names from the manifest), README.md (headline).
#
#   Any args after the run-dir are passed through to the report (e.g. --no-billing).
#
# The heavy raw per-run folders stay in study/ (gitignored); only results/<name>/ is committed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

NAME="${1:-}"
[ -n "$NAME" ] || { echo "usage: ./create_study.sh <name> [run-dir] [report-args...]" >&2; exit 1; }
shift

STUDY="$HERE/study/$NAME"

# --- 1. resolve / create study/<name> -------------------------------------------------------------
if [ -d "$STUDY" ]; then
  echo "study:  study/$NAME (existing)"
  # if the next arg literally points at this study dir, consume it; otherwise leave args for the report
  [ "${1:-}" = "$STUDY" ] || [ "${1:-}" = "study/$NAME" ] && shift || true
else
  if [ "${1:-}" ] && [ -d "${1:-}" ]; then SRC="$1"; shift
  else SRC="$(ls -1dt "$HERE"/study/*/ 2>/dev/null | head -1 | sed 's:/$::')"; fi
  [ -n "${SRC:-}" ] && [ -d "$SRC" ] || { echo "no run under study/ (and none given) to name" >&2; exit 1; }
  echo "study:  naming $SRC -> study/$NAME"
  mv "$SRC" "$STUDY"
fi

# --- 2. generate the report on the raw run --------------------------------------------------------
echo "report: benchmark_progress_report.py study/$NAME $*"
python3 "$HERE/benchmark_progress_report.py" "$STUDY" "$@"

# --- 3. freeze the light artifacts into results/<name> --------------------------------------------
DST="$HERE/results/$NAME"
mkdir -p "$DST"
for f in manifest.csv per_run.csv summary.csv deepswe_task_difficulty.csv billing.json progress_report.html; do
  if [ -f "$STUDY/$f" ]; then cp "$STUDY/$f" "$DST/"; else echo "  (missing, skipped: $f)"; fi
done

# tasks.txt — the task names actually run, from the manifest
python3 - "$STUDY/manifest.csv" "$DST/tasks.txt" <<'PY'
import csv, sys
tasks = sorted({r["task"] for r in csv.DictReader(open(sys.argv[1])) if r.get("task")})
with open(sys.argv[2], "w") as f:
    f.write(f"# {len(tasks)} tasks in this study (one per line)\n")
    f.write("\n".join(tasks) + "\n")
print(f"  tasks.txt: {len(tasks)} tasks")
PY

# README.md — headline pulled from summary.csv (per-setup rollup)
python3 - "$DST" "$NAME" <<'PY'
import csv, os, sys
dst, name = sys.argv[1], sys.argv[2]
rows = {r["setup"]: r for r in csv.DictReader(open(os.path.join(dst, "summary.csv")))}
out = [f"# Study: {name}", "",
       "Frozen, committable results (light artifacts). The raw per-run logs stay under `study/` "
       "(gitignored); regenerate them by re-running the benchmark.", "",
       "| Setup | pass@k (tasks) | pass@1 (attempts) | $/attempt | $/completed task |",
       "|---|---|---|---|---|"]
for s in ("opus", "glm-default", "glm-high", "glm-nothink"):
    r = rows.get(s)
    if not r:
        continue
    out.append(f"| {s} | {r['tasks_solved']}/{r['tasks_total']} | {r['pass_runs']}/{r['runs']} "
               f"| ${float(r['avg_usd_per_run']):.2f} | ${float(r['usd_per_task_solved']):.2f} |")
out += ["", "Files: `tasks.txt`, `manifest.csv`, `per_run.csv`, `summary.csv`, "
        "`deepswe_task_difficulty.csv`, `billing.json`, `progress_report.html`.", ""]
open(os.path.join(dst, "README.md"), "w").write("\n".join(out) + "\n")
print("  README.md: headline written")
PY

echo "froze -> results/$NAME"
ls -1 "$DST" | sed 's/^/  /'
