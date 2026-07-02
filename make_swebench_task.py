#!/usr/bin/env python3
"""Materialize a REAL SWE-bench Verified instance into tasks/demo-swebench-<id>/.

The instance data (repo, base commit, the test patch that introduces the failing
tests, the problem statement, and the FAIL_TO_PASS test IDs) all come from the
official dataset — nothing is fabricated here.

Usage (run from the repo root):
    pip install datasets
    python3 make_swebench_task.py <instance_id>
    # e.g. python3 make_swebench_task.py psf__requests-2317

Then run it like any other task:
    ./run_bench.sh --runs 1 --models "modal/zai-org/GLM-5.2-FP8"

Caveats (see SWEBENCH.md):
- Each instance needs its repo's own deps. The prompt tells the agent to set up a
  venv; heavy scientific repos (numpy/scipy/astropy) may be slow or flaky locally.
  For the rigorous leaderboard number use the official SWE-bench harness (Docker).
- Pick lightweight repos first (requests, flask, click, pytest) for quick runs.
"""
import json
import pathlib
import sys


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: make_swebench_task.py <instance_id>   e.g. psf__requests-2317")
    iid = sys.argv[1]

    try:
        from datasets import load_dataset
    except ImportError:
        sys.exit("missing dependency: pip install datasets")

    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    row = next((r for r in ds if r["instance_id"] == iid), None)
    if row is None:
        sys.exit(f"instance {iid!r} not found in SWE-bench_Verified")

    repo = row["repo"]                       # e.g. "psf/requests"
    base = row["base_commit"]
    url = f"https://github.com/{repo}.git"
    f2p = row["FAIL_TO_PASS"]
    if isinstance(f2p, str):
        f2p = json.loads(f2p)

    d = pathlib.Path(f"tasks/demo-swebench-{iid}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "repo.git").write_text(f"{url} {base}\n")
    (d / "test.patch").write_text(row["test_patch"])
    (d / "f2p.txt").write_text("\n".join(f2p) + "\n")

    # setup: apply the dataset's test patch so the failing tests exist. The patch
    # lives in the task dir (absolute path baked in — setup.sh runs in the work dir).
    patch = (d / "test.patch").resolve()
    (d / "setup.sh").write_text(
        "#!/usr/bin/env bash\n"
        "# apply the SWE-bench test patch (adds/updates the failing tests)\n"
        "set -euo pipefail\n"
        f'git apply "{patch}"\n'
        'echo "applied SWE-bench test patch"\n'
    )
    # verify: the FAIL_TO_PASS tests must pass.
    tests = " ".join(f2p)
    (d / "verify.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'root="$(pwd)"; py="$root/.venv/bin/python"; [ -x "$py" ] || py=python3\n'
        f'"$py" -m pytest {tests} -q\n'
    )
    (d / "prompt.txt").write_text(
        row["problem_statement"].strip()
        + "\n\nThis is a fresh clone with no virtualenv. Create `.venv` at the repo "
        "root and install what you need (e.g. `python3 -m venv .venv && "
        ".venv/bin/pip install -e . pytest`). Do not modify the tests. Make the "
        "failing tests pass.\n"
    )
    for f in ("setup.sh", "verify.sh"):
        (d / f).chmod(0o755)

    print(f"wrote {d}  (repo {repo} @ {base[:10]}, {len(f2p)} FAIL_TO_PASS tests)")
    print("run:  ./run_bench.sh --runs 1")


if __name__ == "__main__":
    main()
