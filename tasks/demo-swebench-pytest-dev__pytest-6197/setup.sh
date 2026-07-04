#!/usr/bin/env bash
# apply the SWE-bench test patch (adds/updates the failing tests)
set -euo pipefail
git apply "/Users/zechim/dev/glm-review/tasks/demo-swebench-pytest-dev__pytest-6197/test.patch"
echo "applied SWE-bench test patch"
