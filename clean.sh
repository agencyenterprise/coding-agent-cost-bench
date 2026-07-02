#!/usr/bin/env bash
# Wipe benchmark artifacts so the next run starts clean.
#   ./clean.sh          -> remove results/* (logs, ccusage snapshots, CSVs); keeps results/.gitignore
#   ./clean.sh --all    -> also remove stray temp work dirs from aborted runs
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -d results ]; then
  n=$(find results -mindepth 1 -maxdepth 1 ! -name '.gitignore' | wc -l | tr -d ' ')
  find results -mindepth 1 -maxdepth 1 ! -name '.gitignore' -exec rm -rf {} +
  echo "removed results/ ($n entries, kept .gitignore)"
else
  echo "results/ already clean"
fi

if [ "${1:-}" = "--all" ]; then
  # opencode/bench leftovers from killed runs (safe: only our mktemp pattern)
  found=$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'tmp.*' -type d 2>/dev/null | wc -l | tr -d ' ')
  echo "note: $found tmp.* dirs exist under ${TMPDIR:-/tmp} — not auto-deleted (may not be ours)."
  echo "      remove manually if you're sure: rm -rf ${TMPDIR:-/tmp}/tmp.*"
fi
