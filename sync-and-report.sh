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

REMOTE="${REMOTE:-ubuntu@ec2-98-80-82-162.compute-1.amazonaws.com}"
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

mkdir -p "$LOCAL_RESULTS"
# Stream the tarball from the remote: cd into the results dir, tar just RUN so archive paths
# are RUN/... (decoupled from the remote dir name). --ignore-failed-read tolerates the
# root-owned agent/sessions files (Permission denied) without aborting the whole archive.
echo "downloading ..."
ssh "$REMOTE" "tar czf - --ignore-failed-read -C $REMOTE_RESULTS '$RUN'" > "$HERE/results.tar.gz"

# replace: drop the old copy, then extract fresh
rm -rf "$LOCAL_RESULTS/$RUN"
tar xzf "$HERE/results.tar.gz" -C "$LOCAL_RESULTS"
rm -f "$HERE/results.tar.gz"
echo "extracted -> $LOCAL_RESULTS/$RUN"

python3 "$HERE/benchmark_progress_report.py" "$LOCAL_RESULTS/$RUN"
