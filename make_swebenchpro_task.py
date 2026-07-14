#!/usr/bin/env python3
"""Materialize a REAL SWE-bench Pro instance into tasks/demo-swebenchpro-<short-id>/.

SWE-bench Pro (Scale AI) = the harder SWE-bench: 731 public instances, 11 large OSS repos,
4 languages (python/go/js/ts), long-horizon multi-file fixes. Same task-dir shape as our
Verified tasks (repo.git / test.patch / f2p.txt / prompt.v1-v3 / meta.json), so bench.sh
runs them unchanged. See SWEBENCH_PRO.md.

Data source: scaleapi/SWE-bench_Pro-os ships the full dataset as
helper_code/sweap_eval_full_v2.jsonl (same rows as HF ScaleAI/SWE-bench_Pro, plus the
eval plumbing: image name, run/parse script refs, before_repo_set_cmd).

    git clone --depth 1 https://github.com/scaleapi/SWE-bench_Pro-os .cache/SWE-bench_Pro-os
    python3 make_swebenchpro_task.py <instance_id or unique substring, e.g. a commit sha>

Grading is NOT wired yet (Verified-only swe_eval_modal.py doesn't know these instances):
tasks generate fine today; grade with the Pro adapter when added (SWEBENCH_PRO.md step 2).
Tests are hidden during generation, exactly like the Verified tasks: no setup.sh, no
verify.sh, test.patch is applied only at grade time inside the instance's Docker image
(public mirror: jefzda/sweap-images:<dockerhub_tag>, in meta.json).
"""
import json
import os
import pathlib
import re
import sys

SCRIPTS_ROOTS = [".cache/SWE-bench_Pro-os", os.path.expanduser("~/.cache/SWE-bench_Pro-os")]
JSONL_CANDIDATES = [f"{r}/helper_code/sweap_eval_full_v2.jsonl" for r in SCRIPTS_ROOTS]

REPO_LANG = {  # HF `repo_language` column, keyed by repo (each Pro repo is single-language)
    "qutebrowser/qutebrowser": "python", "ansible/ansible": "python",
    "internetarchive/openlibrary": "python",
    "flipt-io/flipt": "go", "gravitational/teleport": "go",
    "navidrome/navidrome": "go", "future-architect/vuls": "go",
    "NodeBB/NodeBB": "js",
    "protonmail/webclients": "ts", "element-hq/element-web": "ts", "tutao/tutanota": "ts",
}

# Test-runner used in the v2/v3 prompt's success line. Grading uses the instance's OFFICIAL
# run script inside its Docker image regardless — this line only orients the agent.
REPO_TEST_CMD = {
    "NodeBB/NodeBB": "npx mocha {files}",
    "element-hq/element-web": "yarn jest {files}",
    "protonmail/webclients": "yarn test {files}",
    "tutao/tutanota": "npm test",
}

ENV_NOTES = {
    "python": ("- Fresh clone, no virtualenv. Create `.venv` at the repo root.\n"
               "- Install only what is required to run the tests: `.venv/bin/pip install -e . pytest`.\n"
               "  Do not install anything else.\n"),
    "go":     ("- Fresh clone. A Go toolchain is assumed on PATH; `go build ./...` should compile.\n"
               "- Do not add new module dependencies.\n"),
    "js":     ("- Fresh clone. Node.js is assumed on PATH; run `npm install` once if needed.\n"
               "- Do not add new package dependencies.\n"),
    "ts":     ("- Fresh clone. Node.js is assumed on PATH; run the repo's package-manager install "
               "(`yarn install` / `npm install`) once if needed.\n"
               "- Do not add new package dependencies.\n"),
}


def _unjson(s):
    """Many Pro rows store text fields quote-wrapped with literal \\n escapes (not valid JSON —
    inner quotes are unescaped; the HF viewer shows the same). Unwrap + unescape those;
    pass plain rows through untouched."""
    if isinstance(s, str) and s.startswith('"') and s.rstrip().endswith('"'):
        inner = s.rstrip()[1:-1]
        return inner.replace("\\n", "\n").replace("\\t", "\t")
    return s


def _load_rows():
    for p in JSONL_CANDIDATES:
        if os.path.exists(p):
            with open(p) as f:
                return [json.loads(l) for l in f]
    sys.exit("dataset jsonl not found — run:\n  git clone --depth 1 "
             "https://github.com/scaleapi/SWE-bench_Pro-os .cache/SWE-bench_Pro-os")


def _dockerhub_tag(row):
    """ECR image_name -> public DockerHub tag under jefzda/sweap-images.
    .../sweap-images/nodebb.nodebb:NodeBB__NodeBB-<sha>[-v<hash>] -> nodebb.nodebb-NodeBB__NodeBB-<sha>[-v<hash>]
    (matches Scale's helper_code/image_uri.py, incl. its 128-char DockerHub tag cap)"""
    path, tag = row["image_name"].rsplit(":", 1)
    return f"{path.rsplit('/', 1)[-1]}-{tag}"[:128]


def _vendor_eval_assets(d, row):
    """Copy the instance's OFFICIAL grading assets (from the scaleapi/SWE-bench_Pro-os clone) into
    tasks/<dir>/pro_eval/, so grading (swe_pro_eval_modal.py) is self-contained: run_script.sh +
    parser.py verbatim, env.sh = the ENV lines of both dockerfiles as exports (what Scale's
    entryscript does), p2p.json = PASS_TO_PASS (resolved iff f2p AND p2p all pass)."""
    iid = row["instance_id"]
    root = next((r for r in SCRIPTS_ROOTS if os.path.isdir(f"{r}/run_scripts/{iid}")), None)
    if root is None:
        print(f"WARNING: no run_scripts/{iid} in {SCRIPTS_ROOTS} — task not gradable until vendored")
        return
    ev = d / "pro_eval"
    ev.mkdir(exist_ok=True)
    for name in ("run_script.sh", "parser.py"):
        (ev / name).write_text(open(f"{root}/run_scripts/{iid}/{name}").read())
    envs = []
    for kind in ("base_dockerfile", "instance_dockerfile"):
        p = f"{root}/dockerfiles/{kind}/{iid}/Dockerfile"
        if os.path.exists(p):
            envs += [ln.strip().replace("ENV", "export", 1)
                     for ln in open(p) if ln.strip().startswith("ENV")]
    (ev / "env.sh").write_text("\n".join(envs) + "\n")
    p2p = row["PASS_TO_PASS"]
    if isinstance(p2p, str):
        p2p = json.loads(p2p)
    (ev / "p2p.json").write_text(json.dumps(p2p, indent=0) + "\n")


def _suite_cmd(row, lang, f2p):
    """The 'success criteria' command shown to the agent (file/name level, never test bodies)."""
    if lang == "python":
        files = " ".join(sorted({x.split("::")[0] for x in f2p}))
        return f".venv/bin/python -m pytest {files}"
    if lang == "go":
        names = sorted({x.split("/")[0] for x in f2p})       # top-level Test* names
        return "go test ./... -run '" + "|".join(names[:20]) + "'"
    sel = row.get("selected_test_files_to_run") or []
    if isinstance(sel, str):
        sel = json.loads(sel) if sel.strip().startswith("[") else [sel]
    # keep real test-file paths: strip the image's /app/ prefix, drop snapshots & bare test names
    paths = sorted({s.removeprefix("/app/") for s in sel if "/" in s and not s.endswith(".snap")})
    paths = [p for p in paths if not any(q != p and q.endswith(p) for q in paths)]  # drop suffix dupes
    files = " ".join(paths)
    return REPO_TEST_CMD.get(row["repo"], "npm test").format(files=files).strip()


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: make_swebenchpro_task.py <instance_id or unique substring>")
    key = sys.argv[1]
    rows = [r for r in _load_rows() if key in r["instance_id"]]
    if len(rows) != 1:
        sys.exit(f"{key!r} matched {len(rows)} instances — need exactly 1")
    row = rows[0]

    repo, base, iid = row["repo"], row["base_commit"], row["instance_id"]
    lang = REPO_LANG.get(repo, "?")
    f2p = row["FAIL_TO_PASS"]
    if isinstance(f2p, str):
        f2p = json.loads(f2p)

    # ids run 65-120 chars: short dir = org__repo + first 12 of the fix commit in the id
    sha = re.search(r"-([0-9a-f]{40})", iid).group(1)
    d = pathlib.Path(f"tasks/demo-swebenchpro-{repo.replace('/', '__')}-{sha[:12]}")
    d.mkdir(parents=True, exist_ok=True)

    (d / "repo.git").write_text(f"https://github.com/{repo}.git {base}\n")
    (d / "test.patch").write_text(row["test_patch"])
    (d / "f2p.txt").write_text("\n".join(f2p) + "\n")
    (d / "meta.json").write_text(json.dumps({
        "instance_id": iid, "repo": repo, "version": str(row.get("version", "")),
        "difficulty": "",                                   # Pro has no Verified-style tier
        "benchmark": "swebench_pro", "repo_language": lang,
        "dockerhub_tag": _dockerhub_tag(row),               # jefzda/sweap-images:<this>
        "before_repo_set_cmd": _unjson(row.get("before_repo_set_cmd", "")),
        "selected_test_files_to_run": _unjson(row.get("selected_test_files_to_run", [])),
    }, indent=2) + "\n")
    _vendor_eval_assets(d, row)

    if lang == "python":
        bad = [x for x in f2p if "::" not in x]
        if bad:
            print(f"WARNING: {len(bad)}/{len(f2p)} FAIL_TO_PASS ids don't look like pytest node ids")

    statement = _unjson(row["problem_statement"]).strip()
    suite = _suite_cmd(row, lang, f2p)
    env = ENV_NOTES.get(lang, ENV_NOTES["js"])

    # v1 = the original problem statement, verbatim (raw-issue baseline, as in Verified tasks)
    (d / "prompt.v1.txt").write_text(statement + "\n")
    # v2 = shaped uniform template (same skeleton as Verified v2, language-aware env/success)
    issue = "\n".join(("> " + ln) if ln.strip() else ">" for ln in statement.splitlines())
    (d / "prompt.v2.txt").write_text(
        "## Task\n"
        "Reported issue (verbatim):\n\n"
        f"{issue}\n\n"
        "## Success criteria\n"
        f"- `{suite}` exits 0 with all tests passing.\n"
        "- Do not modify any test files.\n\n"
        "## Scope\n"
        "- Make the smallest change that fully fixes the issue.\n"
        "- If the same defect appears in more than one place (e.g. serialization AND deserialization "
        "paths, or multiple call sites), fix every occurrence.\n"
        "- Do not refactor, reformat, modernize, upgrade dependencies, or fix unrelated issues.\n\n"
        "## Environment\n"
        f"{env}\n"
        "## Before finishing\n"
        "- Run the Success criteria command. If anything fails, keep working.\n"
        "- Confirm your diff contains only changes required by the fix.\n"
    )
    # v3 = raw issue + only the operational scaffolding (control)
    (d / "prompt.v3.txt").write_text(
        statement + "\n\n---\n"
        f"{env.replace('- ', '').replace(chr(10), ' ').strip()} "
        f"Make the failing tests pass, then confirm with `{suite}` (exit 0) before finishing.\n"
    )

    print(f"wrote {d}  ({repo} @ {base[:10]}, {lang}, {len(f2p)} FAIL_TO_PASS tests)")


if __name__ == "__main__":
    main()
