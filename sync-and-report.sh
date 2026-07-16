#!/usr/bin/env bash
# Pull a benchmark run from the AWS box, replace the local copy under ./results/, and
# render the interim progress report (report.html, CSV, console tables) — safe to run
# while the benchmark is still going; it just reports on whatever runs exist so far.
#
# Usage:  ./sync-and-report.sh [RUN_ID]
#   RUN_ID  results-folder name on the remote (default: newest dir under the remote results/).
# Config comes from .env (REMOTE, REMOTE_RESULTS); env vars override.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }

REMOTE="${REMOTE}"
REMOTE_RESULTS="${REMOTE_RESULTS:-results}"
LOCAL_RESULTS="$HERE/results"

RUN="${1:-}"
if [ -z "$RUN" ]; then
  echo "resolving newest run on $REMOTE ..."
  # newest sub-DIRECTORY by mtime (ignore stray files like proxy.log); basename only
  RUN="$(ssh "$REMOTE" "ls -1dt $REMOTE_RESULTS/*/ 2>/dev/null | head -1 | xargs -n1 basename")"
fi
[ -n "$RUN" ] || { echo "no run id" >&2; exit 1; }
echo "run: $RUN"

TARBALL="$LOCAL_RESULTS/results.tar.gz"
mkdir -p "$LOCAL_RESULTS"
# Stream the tarball from the remote: cd into the results dir, tar just RUN so archive paths
# are RUN/... (decoupled from the remote dir name). EXCLUDE pier-jobs — it's live pier scratch
# (task-container fs / root-owned agent sessions) the report never reads; pulling it mid-run is
# what triggered the "Permission denied" warnings and shipped GBs of scratch. Excluding it means
# there's nothing unreadable left to tar, so the sync is clean and small.
echo "downloading ..."
# Byte counter on stderr so you can see the stream moving (no pv required).
ssh "$REMOTE" "tar czf - --exclude='$RUN/pier-jobs' --ignore-failed-read -C $REMOTE_RESULTS '$RUN'" \
  | python3 -c '
import sys
n = 0
while True:
    b = sys.stdin.buffer.read(1 << 20)
    if not b:
        break
    sys.stdout.buffer.write(b)
    n += len(b)
    sys.stderr.write(f"\r  {n / (1 << 20):.1f} MB")
    sys.stderr.flush()
sys.stderr.write("\n")
' > "$TARBALL"

# replace: drop the old copy, then extract fresh
rm -rf "$LOCAL_RESULTS/$RUN"
tar xzf "$TARBALL" -C "$LOCAL_RESULTS"
rm -f "$TARBALL"
echo "extracted -> $LOCAL_RESULTS/$RUN"

python3 "$HERE/benchmark_progress_report.py" "$LOCAL_RESULTS/$RUN"
