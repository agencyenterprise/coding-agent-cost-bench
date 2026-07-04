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

    # setup/verify run in the WORK dir but their aux files (test.patch, f2p.txt) live next to the
    # script in the task dir. Resolve that dir at RUNTIME from $BASH_SOURCE — never bake an absolute
    # path (these are committed demo tasks and must be portable across machines).
    here = 'here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
    (d / "setup.sh").write_text(
        "#!/usr/bin/env bash\n"
        "# apply the SWE-bench test patch (adds/updates the failing tests)\n"
        "set -euo pipefail\n"
        + here
        + 'git apply "$here/test.patch"\n'
        'echo "applied SWE-bench test patch"\n'
    )
    # verify: the FAIL_TO_PASS tests must pass. Read node ids from f2p.txt one-per-line into an
    # array (space-safe: some ids contain spaces, e.g. pytest params like `[a: int]`), so a naive
    # space-join doesn't split them into broken args.
    (d / "verify.sh").write_text(
        "#!/usr/bin/env bash\n"
        "# run the SWE-bench FAIL_TO_PASS tests (node ids from f2p.txt, space-safe)\n"
        "set -e\n"
        + here
        + 'root="$(pwd)"; py="$root/.venv/bin/python"; [ -x "$py" ] || py=python3\n'
        "tests=()\n"
        'while IFS= read -r t; do [ -n "$t" ] && tests+=("$t"); done < "$here/f2p.txt"\n'
        '"$py" -m pytest "${tests[@]}" -q\n'
    )
    bad = [x for x in f2p if "::" not in x or " " in x or x.count("[") != x.count("]")]
    if bad:
        print(f"WARNING: {len(bad)}/{len(f2p)} FAIL_TO_PASS ids for {iid} look malformed "
              f"(not a pytest node id, or a space / unbalanced brackets from dataset mangling) — "
              f"e.g. {bad[0]!r}. verify.sh likely can't resolve them, so the task may never pass. "
              "Pick a different instance.")
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
