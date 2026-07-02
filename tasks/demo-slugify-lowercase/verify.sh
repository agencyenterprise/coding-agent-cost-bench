#!/usr/bin/env bash
# Success = python-slugify's test suite passes.
set -e
root="$(pwd)"
py="$root/.venv/bin/python"
[ -x "$py" ] || py=python3
"$py" -m pytest test.py -q
