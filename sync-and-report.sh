#!/usr/bin/env bash
# Pull a benchmark run from the AWS box, replace the local copy under ./study/, and
# render the interim progress report — safe to run while the benchmark is still going.
#
# Usage:  ./sync-and-report.sh [RUN_ID]
#   RUN_ID  study-folder name on the remote (default: newest dir under the remote study/).
# Config comes from .env (REMOTE, REMOTE_STUDY); env vars override.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }

REMOTE="${REMOTE}"
REMOTE_STUDY="${REMOTE_STUDY:-study}"
LOCAL_STUDY="$HERE/study"
STEPS=3

step() {
  # [ date ] i/N msg (done)
  printf '[ %s ] %d/%d %s (done)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$STEPS" "$2"
}

RUN="${1:-}"
if [ -z "$RUN" ]; then
  RUN="$(ssh "$REMOTE" "ls -1dt $REMOTE_STUDY/*/ 2>/dev/null | head -1 | xargs -n1 basename")"
fi
[ -n "$RUN" ] || { echo "no run id" >&2; exit 1; }
step 1 "resolve $RUN"

TARBALL="$LOCAL_STUDY/$RUN.tar.gz"
mkdir -p "$LOCAL_STUDY"
# Stream the tarball from the remote: cd into the study dir, tar just RUN so archive paths
# are RUN/... (decoupled from the remote dir name). EXCLUDE pier-jobs — it's live pier scratch
# (task-container fs / root-owned agent sessions) the report never reads; pulling it mid-run is
# what triggered the "Permission denied" warnings and shipped GBs of scratch. Excluding it means
# there's nothing unreadable left to tar, so the sync is clean and small.
#
# Mid-run: tar may warn "file changed as we read it" (bench still writing) and exit 1 — that's OK.
# Progress MB uses \r; tar warnings are forced onto their own line so they don't glue to it.
set +e
ssh "$REMOTE" "tar czf - --exclude='$RUN/pier-jobs' --ignore-failed-read -C $REMOTE_STUDY '$RUN'" \
  2> >(while IFS= read -r line || [ -n "$line" ]; do printf '\r\033[K%s\n' "$line" >&2; done) \
  | python3 -c '
import sys
n = 0
while True:
    b = sys.stdin.buffer.read(1 << 20)
    if not b:
        break
    sys.stdout.buffer.write(b)
    n += len(b)
    sys.stderr.write(f"\r\033[K  {n / (1 << 20):.1f} MB")
    sys.stderr.flush()
sys.stderr.write("\n")
' > "$TARBALL"
ec=${PIPESTATUS[0]}
set -e
# 0 = clean; 1 = warnings (changed files / skipped reads) while archive is still usable
[ "$ec" -eq 0 ] || [ "$ec" -eq 1 ] || exit "$ec"
MB="$(python3 -c "import os; print(f'{os.path.getsize(\"$TARBALL\") / (1 << 20):.1f}')")"
step 2 "download ${MB} MB"

# replace: drop the old copy, then extract fresh
rm -rf "$LOCAL_STUDY/$RUN"
tar xzf "$TARBALL" -C "$LOCAL_STUDY"
rm -f "$TARBALL"
step 3 "extract → $LOCAL_STUDY/$RUN"

python3 "$HERE/benchmark_progress_report.py" "$LOCAL_STUDY/$RUN"
