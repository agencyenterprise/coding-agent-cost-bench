#!/usr/bin/env bash
# Success = click's parser tests pass.
set -e
root="$(pwd)"; py="$root/.venv/bin/python"
# Self-sufficient grader: use the agent's .venv only if it has pytest; else provision a throwaway
# grader venv (editable-install the package so the agent's fix is under test) and run there.
if ! { [ -x "$py" ] && "$py" -c 'import pytest'; } >/dev/null 2>&1; then
  gv="$(mktemp -d)/venv"; python3 -m venv "$gv" >/dev/null 2>&1; py="$gv/bin/python"
  "$py" -m pip install -q -e . pytest >/dev/null 2>&1
fi
"$py" -m pytest tests/test_parser.py -q
