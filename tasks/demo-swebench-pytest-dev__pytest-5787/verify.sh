#!/usr/bin/env bash
# run the SWE-bench FAIL_TO_PASS tests (node ids from f2p.txt, space-safe)
set -e
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(pwd)"; py="$root/.venv/bin/python"
# Self-sufficient grader: use the agent's .venv only if it has pytest; else provision a throwaway
# grader venv, editable-installing the repo (== the pytest under test) so the agent's fix is active.
# (Old pytest needs Python <=3.11; run via ./run_on_docker.sh — the host's 3.14 crashes old pytest.)
if ! { [ -x "$py" ] && "$py" -c 'import pytest'; } >/dev/null 2>&1; then
  gv="$(mktemp -d)/venv"; python3 -m venv "$gv" >/dev/null 2>&1; py="$gv/bin/python"
  "$py" -m pip install -q -e . pytest >/dev/null 2>&1
fi
tests=()
while IFS= read -r t; do [ -n "$t" ] && tests+=("$t"); done < "$here/f2p.txt"
"$py" -m pytest "${tests[@]}" -q
