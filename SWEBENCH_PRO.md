# Adding SWE-bench Pro tasks (research + integration plan)

> **Status: fully wired.** `make_swebenchpro_task.py` + 10 mixed-language tasks
> (`tasks/demo-swebenchpro-*`: 4 python, 3 go, 1 js, 2 ts) + `swe_pro_eval_modal.py` (grading).
> `./grade_swe.sh --results-dir <dir>` now grades BOTH benchmarks: the Verified grader skips Pro
> instances, the Pro grader skips Verified ones, both merge into the same `resolved.json`.
> Each task dir vendors its official grading assets under `pro_eval/` (run_script.sh, parser.py,
> env.sh = the image dockerfiles' ENV lines, p2p.json), so grading needs no dataset access.
>
> **Validate before trusting** (needs Modal auth; ~10 sandbox-minutes):
> `python3 swe_pro_eval_modal.py --gold` — grades the dataset's gold patches for all Pro tasks;
> every line must say RESOLVED. Locally verified so far: gold patch + test-file checkout apply
> cleanly at base_commit (navidrome spot check).
>
> Notes: the full dataset ships inside the eval repo as `helper_code/sweap_eval_full_v2.jsonl`
> (same rows as HF plus image/run-script refs) — no HF download needed:
> `git clone --depth 1 https://github.com/scaleapi/SWE-bench_Pro-os .cache/SWE-bench_Pro-os`.
> Quirk: many rows quote-wrap text fields with literal `\n` escapes — the generator undoes this.
> Resolved criterion (Scale's): (FAIL_TO_PASS ∪ PASS_TO_PASS) ⊆ passed; tests are staged not by
> applying test_patch but by checking the dataset's test files out of the fix commit (the last
> line of `before_repo_set_cmd`), exactly like the official harness.

SWE-bench Pro (Scale AI, 2025) is the "harder SWE-bench": 731 public instances from 11 large
OSS repos (NodeBB, qutebrowser, ansible, teleport, navidrome, openlibrary, tutao, ...), 4
languages (**python, go, js, ts**), long-horizon multi-file fixes with human-verified tests.
It deliberately mirrors the Verified schema, which is why it slots into our task format well.

- Dataset: <https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro> (`ScaleAI/SWE-bench_Pro`, split `test`, 731 rows)
- Eval harness: <https://github.com/scaleapi/SWE-bench_Pro-os> (`swe_bench_pro_eval.py`, supports **Modal** natively or local docker)
- Per-instance eval images: DockerHub `jefzda/sweap-images:{dockerhub_tag}` (a `dockerhub_tag` column per row)
- Leaderboard: <https://scale.com/leaderboard/swe_bench_pro_public> — published Claude baselines exist here too

## Schema mapping (Pro row -> our task dir)

Same core fields as Verified, so `make_swebench_task.py`'s shape carries over:

| Ours | Verified field | Pro field | Notes |
|---|---|---|---|
| `repo.git` | repo + base_commit | `repo` + `base_commit` | same; repos are public GitHub, clone works |
| `test.patch` | test_patch | `test_patch` | same |
| `f2p.txt` | FAIL_TO_PASS | `fail_to_pass` | **NOT always pytest node ids** — go/js use test names |
| `prompt.v1.txt` | problem_statement | `problem_statement` | same (Pro also has `requirements` + `interface` — richer spec, see below) |
| `meta.json` | version/difficulty | `repo_language`, `issue_categories`, `issue_specificity`, `dockerhub_tag`, `before_repo_set_cmd`, `selected_test_files_to_run` | keep all — grader needs `dockerhub_tag` |

Extra Pro fields with no Verified equivalent: `requirements` (behavioral spec), `interface`
(function/class signatures expected), `pass_to_pass`, `before_repo_set_cmd` (git commands the
grader runs to stage the test files), `selected_test_files_to_run`.

## What breaks if we just point make_swebench_task.py at the Pro parquet

1. **Grading is a different harness.** `swe_eval_modal.py` is Verified-only: it uses the
   `swebench` lib's `make_test_spec` + `MAP_REPO_TO_PARSER` and `swebench/sweb.eval.x86_64.*`
   images. None of that knows Pro repos/instances. Pro ships its own `swe_bench_pro_eval.py`
   (already Modal-based!) with per-repo `run_scripts/` (run + parse) and the `jefzda/sweap-images`
   images. We adapt/vendor theirs instead of extending ours.
2. **Prediction format differs.** Ours: `predictions.jsonl` (`model_patch`). Theirs: a JSON list
   of `{instance_id, patch, prefix}`. Trivial converter.
3. **prompt.v2's success criterion is pytest-only.** `.venv/bin/python -m pytest <files>` is wrong
   for go/js/ts instances. Make the suite line language-aware from `repo_language` +
   `selected_test_files_to_run` (`go test ./...`-style / `npx mocha <files>` / pytest).
4. **The f2p sanity check** (`"::" in id`) would flag every go/js instance. Gate it on
   `repo_language == "python"`.
5. **Local generation environment.** Verified repos are pip-installable on the host; Pro repos
   (NodeBB needs Node+Redis/Mongo, teleport needs a Go toolchain, tutao is a monorepo) mostly
   are not. That's acceptable — tests are hidden anyway and the agent works from the issue — but
   agents that try to run the suite will fail more. Optional later: generate inside the
   instance's `jefzda` image via `run_on_docker.sh` for a fairer environment.

## Integration plan (small diffs, same three-step flow)

1. **`make_swebench_task.py --dataset pro <instance_id>`** (or a sibling `make_swebenchpro_task.py`):
   load `ScaleAI/SWE-bench_Pro` (same `datasets`-or-cached-parquet fallback), write
   `tasks/demo-swebenchpro-<short-id>/` with the mapping above. Keep `swebench` in the dir name so
   `make_predictions.py` harvests it unchanged. Truncate the dir name (ids run 65–120 chars —
   use `repo + first 8 of the commit`), keep the full `instance_id` in `meta.json`.
   - v1 = `problem_statement` verbatim (as today).
   - v2 = current template with a language-aware test command; optionally append Pro's
     `requirements`/`interface` blocks — they're part of the official task spec (the leaderboard
     agents see them), not a hint we invented.
2. **`swe_pro_eval_modal.py`**: thin adapter around scaleapi's `swe_bench_pro_eval.py`
   (vendor the script + `run_scripts/` dir, MIT licensed). Input: our `predictions.jsonl`
   converted to their JSON; images pulled as Modal Sandboxes from `jefzda/sweap-images:<tag>`
   (tag from `meta.json`), exactly like today's Sandbox flow. Output: normalize their results
   back into `resolved.json` so `aggregate.py` needs no changes.
3. **`grade_swe.sh`**: route per task — `meta.json` has `dockerhub_tag` ⇒ Pro grader, else the
   existing Verified grader. Both write into the same `resolved.json`.
4. **Validate before trusting** (same ritual as SWEBENCH.md): run each new instance's **gold**
   `patch` through the Pro grader on Modal, expect RESOLVED. Only then add it to the matrix.
5. **Report**: `aggregate.py` already reads `meta.json`; surface `repo_language` +
   `issue_categories` where Verified shows `difficulty`.

## Suggested rollout

- **Phase 1 (cheap, ~1 day):** 5–10 **python** Pro instances (qutebrowser / ansible /
  openlibrary). Python-only avoids issues 3–5 almost entirely; only the grader adapter (step 2)
  is real work. Instant difficulty bump: top models resolve ~55–60% on Pro vs ~75%+ on Verified.
- **Phase 2:** add go (navidrome, teleport) and js/ts (NodeBB, tutao) instances — exercises the
  language-aware prompt + parser paths. Note the 2/9 news: some unit tests were removed as
  outdated (year-2025-dependent); re-pull `run_scripts` before grading, and prefer non-tutao
  instances first (tutao had eval-time issues, fixed 1/7).
- **Pick hard instances deliberately:** filter rows by `issue_categories` / patch size — Pro
  patches run up to 180k chars, so there's real headroom above our current demo set.

## Caveats

- Gold `patch` for Pro is in the public HF dataset (like Verified) — contamination caveats
  identical to today.
- `claude:` harness rows remain comparable: Anthropic models have published Pro numbers on the
  Scale leaderboard to cite next to ours.
- Their harness assumes `bash` as the image default entrypoint (their issue #6) — don't wrap
  commands in an extra `bash -c` in the Sandbox.
