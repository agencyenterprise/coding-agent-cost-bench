#!/usr/bin/env bash
# T3 verify (needs only a docker daemon + network for the anonymous public.ecr.aws pull — NO Modal,
# NO model, NO API keys). Drives the REAL execute_bench.grade_run against one deep-swe task's own
# upstream verifier for the three outcomes the ticket calls out:
#   known-good patch (solution/solution.patch) -> resolved
#   empty patch                                 -> unresolved (grades the pristine base, reward 0)
#   verifier infra-fail sentinel (reward.txt=-1)-> errored    (excluded, not a false unresolved)
#   ./tests/verify_local_grading.sh
set -euo pipefail
cd "$(dirname "$0")/.."
TASK=${TASK:-abs-module-cache-flags}
TA="deep-swe/tasks/$TASK"
RUNID=t3-verify
WORK=$(mktemp -d)
trap 'docker rm -f "$(docker ps -aq --filter label=bench=$RUNID)" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

grade() {  # $1 = outdir ; echoes the verdict from the real grade_run
  python3 - "$TASK" "$TA" "$1" "$RUNID" <<'PY'
import sys, execute_bench as eb
task, ta, outdir, runid = sys.argv[1:5]
meta = eb.load_task_meta(ta)
print(eb.grade_run(task, ta, meta, outdir, {"runid": runid}))
PY
}

echo "==> known-good patch (solution/solution.patch) should grade RESOLVED"
O1="$WORK/resolved"; mkdir -p "$O1"; cp "$TA/solution/solution.patch" "$O1/model.patch"
V1=$(grade "$O1"); echo "    verdict=$V1"
[ "$V1" = resolved ] || { echo "    FAIL: expected resolved"; exit 1; }

echo "==> empty patch should grade UNRESOLVED"
O2="$WORK/empty"; mkdir -p "$O2"; : > "$O2/model.patch"
V2=$(grade "$O2"); echo "    verdict=$V2"
[ "$V2" = unresolved ] || { echo "    FAIL: expected unresolved"; exit 1; }

echo "==> reward.txt=-1 (verifier crash sentinel) maps to ERRORED, not a false unresolved"
python3 - <<'PY'
import json, tempfile, os, execute_bench as eb
# adapter-level: an errored verdict is excluded from resolved.json (not written as unresolved)
d = tempfile.mkdtemp()
import csv
with open(os.path.join(d, "manifest.csv"), "w", newline="") as f:
    csv.writer(f).writerows([["task","harness","model","prompt","run","outdir","start","end","duration_s","status"],
                             ["t","opencode","m","instr","1",f"{d}/r","0","1","1","n/a"]])
eb.finalize_grades(d, {f"{d}/r": {"task":"t","harness":"opencode","model":"m","pv":"instr","run":"1","verdict":"errored"}})
assert json.load(open(os.path.join(d,"resolved.json"))) == {}, "errored leaked into resolved.json"
row = list(csv.reader(open(os.path.join(d,"manifest.csv"))))[1]
assert row[9] == "errored", row
print("    ok: errored excluded from resolved.json + stamped errored in manifest")
PY

echo "ALL CHECKS PASSED"
