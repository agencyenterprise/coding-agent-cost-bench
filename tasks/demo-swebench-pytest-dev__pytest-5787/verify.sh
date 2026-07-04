#!/usr/bin/env bash
# run the SWE-bench FAIL_TO_PASS tests (node ids from f2p.txt, space-safe)
set -e
root="$(pwd)"; py="$root/.venv/bin/python"; [ -x "$py" ] || py=python3
tests=()
while IFS= read -r t; do [ -n "$t" ] && tests+=("$t"); done < "/Users/zechim/dev/glm-review/tasks/demo-swebench-pytest-dev__pytest-5787/f2p.txt"
"$py" -m pytest "${tests[@]}" -q
