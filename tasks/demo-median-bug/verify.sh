#!/usr/bin/env bash
# Success = the math utility tests pass (prefer the agent's .venv).
set -e
root="$(pwd)"; py="$root/.venv/bin/python"
# Self-sufficient grader: use the agent's .venv only if it actually has pytest; otherwise provision a
# throwaway grader venv so grading depends on the code FIX, not on the agent installing pytest itself.
if ! { [ -x "$py" ] && "$py" -c 'import pytest'; } >/dev/null 2>&1; then
  gv="$(mktemp -d)/venv"; python3 -m venv "$gv" >/dev/null 2>&1; py="$gv/bin/python"
  "$py" -m pip install -q pytest >/dev/null 2>&1
fi
"$py" -m pytest test_mathutils.py -q
