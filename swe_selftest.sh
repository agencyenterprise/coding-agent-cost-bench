#!/usr/bin/env bash
# Sanity-check the SWE grader on ONE instance, end-to-end on Modal:
#   - grade the dataset's GOLD patch  -> must RESOLVE   (fix present  => FAIL_TO_PASS pass)
#   - grade a NO-OP patch (new file)  -> must NOT resolve (no fix      => FAIL_TO_PASS fail)
# Same instance flipping proves pass/fail detection is real, not defaulting.
#
#   ./swe_selftest.sh                       # default: pallets__flask-5014 (small/fast)
#   ./swe_selftest.sh sympy__sympy-24443    # any Verified instance id
set -euo pipefail
cd "$(dirname "$0")"
IID="${1:-pallets__flask-5014}"

VENV="$PWD/.cache/swe-venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo ">>> building grading venv (swebench + pyarrow + modal)..." >&2
  python3 -m venv "$VENV"; "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q swebench pyarrow modal
fi
PY="$VENV/bin/python"

PRED="$(mktemp /tmp/swe_selftest.XXXXXX)"
"$PY" - "$IID" "$PRED" <<'PYEOF'
import glob, os, json, sys
import pyarrow.parquet as pq
iid, out = sys.argv[1], sys.argv[2]
pq_path = glob.glob(os.path.expanduser(
    "~/.cache/huggingface/hub/datasets--princeton-nlp--SWE-bench_Verified/snapshots/*/data/*.parquet"))[0]
rows = {r["instance_id"]: r for r in pq.read_table(pq_path).to_pylist()}
if iid not in rows:
    sys.exit(f"{iid} not in SWE-bench Verified")
noop = ("diff --git a/ZZZ_grader_selftest.txt b/ZZZ_grader_selftest.txt\nnew file mode 100644\n"
        "index 0000000..0000001\n--- /dev/null\n+++ b/ZZZ_grader_selftest.txt\n@@ -0,0 +1 @@\n+noop\n")
with open(out, "w") as f:
    f.write(json.dumps({"instance_id": iid, "model_patch": rows[iid]["patch"],
                        "model_name_or_path": "GOLD (expect RESOLVED)"}) + "\n")
    f.write(json.dumps({"instance_id": iid, "model_patch": noop,
                        "model_name_or_path": "NO-OP (expect unresolved)"}) + "\n")
print(f">>> instance: {iid}  ({rows[iid]['repo']}, difficulty: {rows[iid].get('difficulty','?')})")
PYEOF

"$PY" swe_eval_modal.py --predictions "$PRED"
rm -f "$PRED"
echo ">>> EXPECTED: GOLD = RESOLVED, NO-OP = unresolved. If so, the grader is trustworthy."
