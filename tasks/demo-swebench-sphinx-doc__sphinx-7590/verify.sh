#!/usr/bin/env bash
# run the SWE-bench FAIL_TO_PASS tests (node ids from f2p.txt, space-safe)
set -e
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(pwd)"; py="$root/.venv/bin/python"; [ -x "$py" ] || py=python3
tests=()
while IFS= read -r t; do [ -n "$t" ] && tests+=("$t"); done < "$here/f2p.txt"
"$py" -m pytest "${tests[@]}" -q
