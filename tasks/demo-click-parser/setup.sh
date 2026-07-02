#!/usr/bin/env bash
# Inject a real bug into click's split_arg_string: the except branch that keeps
# the partial token (on an incomplete quote/escape) is neutered, so those tokens
# get dropped. tests/test_parser.py::test_split_arg_string then fails on the
# "cli 'my file" and "cli my\\" cases. The agent must restore the append.
set -euo pipefail
python3 - <<'PY'
import pathlib
p = pathlib.Path("src/click/parser.py")
t = p.read_text()
old = "        out.append(lex.token)"
assert t.count(old) == 1, "anchor not found — click layout changed; update setup.sh"
p.write_text(t.replace(old, "        pass  # injected bug", 1))
print("injected: except now drops the partial token")
PY
