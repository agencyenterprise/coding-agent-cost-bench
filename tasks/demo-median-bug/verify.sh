#!/usr/bin/env bash
# Success = the math utility tests pass (prefer the agent's .venv).
set -e
root="$(pwd)"; py="$root/.venv/bin/python"; [ -x "$py" ] || py=python3
"$py" -m pytest test_mathutils.py -q
