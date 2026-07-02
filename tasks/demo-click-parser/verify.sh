#!/usr/bin/env bash
# Success = click's parser tests pass.
set -e
root="$(pwd)"
py="$root/.venv/bin/python"
[ -x "$py" ] || py=python3
"$py" -m pytest tests/test_parser.py -q
