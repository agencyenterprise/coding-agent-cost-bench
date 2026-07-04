#!/usr/bin/env bash
# apply the SWE-bench test patch (adds/updates the failing tests)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
git apply "$here/test.patch"
echo "applied SWE-bench test patch"
