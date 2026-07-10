#!/usr/bin/env python3
"""Materialize a REAL SWE-bench Verified instance into tasks/demo-swebench-<id>/.

The instance data (repo, base commit, the test patch that introduces the failing
tests, the problem statement, and the FAIL_TO_PASS test IDs) all come from the
official dataset — nothing is fabricated here.

Usage (run from the repo root):
    python3 make_swebench_task.py <instance_id>     # needs `datasets` OR a cached parquet + `pyarrow`
    # e.g. python3 make_swebench_task.py psf__requests-6028

Then run it like any other task:
    ./bench.sh --runs 1 --models "modal/zai-org/GLM-5.2-FP8"

Emits prompt.v1/v2/v3.txt, setup.sh, repo.git, test.patch, f2p.txt. Graded on Modal (grade_swe.sh) —
no host verify.sh.

Caveats (see SWEBENCH.md):
- Each instance needs its repo's own deps; heavy scientific repos (numpy/scipy) may be slow/flaky.
- Python version: the grader installs a MODERN pytest, so most PURE-PYTHON instances run on a modern
  host (incl. Python 3.14) IF the repo's test files import cleanly there. Instances whose package IS
  pytest (pytest-dev/pytest-*) need Python <=3.11 — run those via ./run_on_docker.sh.
- For the rigorous leaderboard number use the official SWE-bench harness (Docker).
"""
import glob
import json
import os
import pathlib
import sys


def _load_instance(iid):
    """Fetch one SWE-bench Verified row. Prefer the `datasets` lib; else fall back to the cached HF
    parquet via pyarrow — so this works on a bare host (e.g. Python 3.14) without installing datasets."""
    try:
        from datasets import load_dataset
        ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
        return next((r for r in ds if r["instance_id"] == iid), None)
    except ImportError:
        pass
    pats = glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/datasets--princeton-nlp--SWE-bench_Verified/snapshots/*/data/*.parquet"))
    if not pats:
        sys.exit("no `datasets` lib and no cached dataset — `pip install datasets`, or pre-download "
                 "princeton-nlp/SWE-bench_Verified into the HF cache first.")
    try:
        import pyarrow.parquet as pq
    except ImportError:
        sys.exit("dataset is cached but neither `datasets` nor `pyarrow` is installed — "
                 "`pip install pyarrow` (light) or `pip install datasets`.")
    for p in pats:
        for r in pq.read_table(p).to_pylist():
            if r["instance_id"] == iid:
                return r
    return None


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: make_swebench_task.py <instance_id>   e.g. psf__requests-2317")
    iid = sys.argv[1]

    row = _load_instance(iid)
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
    # provenance for the report (aggregate.py reads this — no dataset access needed at report time):
    # the SWE-bench Verified difficulty tier (human est. time-to-fix) + repo/version.
    (d / "meta.json").write_text(json.dumps({
        "instance_id": iid, "repo": repo, "version": str(row.get("version", "")),
        "difficulty": row.get("difficulty", ""),
    }, indent=2) + "\n")

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
    # No host verify.sh: SWE tasks are graded on Modal via the instance's official Docker image
    # (grade_swe.sh -> swe_eval_modal.py -> resolved.json), which reads FAIL_TO_PASS from the dataset.
    # Still sanity-check the ids — the grader keys on them, so malformed ids => the task can't score.
    bad = [x for x in f2p if "::" not in x or " " in x or x.count("[") != x.count("]")]
    if bad:
        print(f"WARNING: {len(bad)}/{len(f2p)} FAIL_TO_PASS ids for {iid} look malformed "
              f"(not a pytest node id, or a space / unbalanced brackets from dataset mangling) — "
              f"e.g. {bad[0]!r}. The Modal grader may never resolve them; pick a different instance.")
    # Two prompt versions (see PROMPTS.md). v1 = prompt.v1.txt = the ORIGINAL problem statement,
    # verbatim, nothing added — the raw GitHub issue a developer would see. v2 = prompt.v2.txt =
    # our shaped uniform template (verbatim issue block + FILE-level suite command + explicit
    # scope/env/checklist). The Modal grader runs the exact FAIL_TO_PASS tests either way, so the
    # prompt is the only thing that changes across versions.
    statement = row["problem_statement"].strip()
    (d / "prompt.v1.txt").write_text(statement + "\n")       # v1: original, unmodified
    issue = "\n".join(("> " + ln) if ln.strip() else ">" for ln in statement.splitlines())
    suite = " ".join(sorted({x.split("::")[0] for x in f2p}))   # the file(s) holding the tests, not the node ids
    (d / "prompt.v2.txt").write_text(
        "## Task\n"
        "Reported issue (verbatim):\n\n"
        f"{issue}\n\n"
        "## Success criteria\n"
        f"- `.venv/bin/python -m pytest {suite}` exits 0 with all tests passing.\n"
        "- Do not modify any test files.\n\n"
        "## Scope\n"
        "- Make the smallest change that fully fixes the issue.\n"
        "- If the same defect appears in more than one place (e.g. serialization AND deserialization "
        "paths, or multiple call sites), fix every occurrence.\n"
        "- Do not refactor, reformat, modernize, upgrade dependencies, or fix unrelated issues.\n\n"
        "## Environment\n"
        "- Fresh clone, no virtualenv. Create `.venv` at the repo root.\n"
        "- Install only what is required to run the tests: `.venv/bin/pip install -e . pytest`.\n"
        "  Do not install anything else.\n\n"
        "## Before finishing\n"
        "- Run the Success criteria command. If anything fails, keep working.\n"
        "- Confirm your diff contains only changes required by the fix.\n"
    )
    # v3 = raw issue (like v1) + only the operational scaffolding (env + verify command), no structure.
    (d / "prompt.v3.txt").write_text(
        statement + "\n\n---\n"
        "Fresh clone, no virtualenv: create `.venv` at the repo root and `.venv/bin/pip install -e . "
        f"pytest`. Make the failing tests pass, then confirm with `.venv/bin/python -m pytest {suite}` "
        "(exit 0) before finishing.\n"
    )
    (d / "setup.sh").chmod(0o755)

    print(f"wrote {d}  (repo {repo} @ {base[:10]}, {len(f2p)} FAIL_TO_PASS tests)")
    print("run:  ./bench.sh --runs 1")


if __name__ == "__main__":
    main()
