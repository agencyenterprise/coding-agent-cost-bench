#!/usr/bin/env bash
# Inject a real bug into python-slugify: skip the lowercasing step. 24 cases in
# test.py then fail (they expect lowercase output). The agent must restore it.
set -euo pipefail
python3 - <<'PY'
import pathlib
p = pathlib.Path("slugify/slugify.py")
t = p.read_text()
old = "        text = text.lower()"
assert t.count(old) == 1, "anchor not found — slugify changed; update setup.sh"
p.write_text(t.replace(old, "        text = text  # injected bug", 1))
print("injected: lowercase step skipped")
PY
