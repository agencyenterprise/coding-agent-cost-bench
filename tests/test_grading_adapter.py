#!/usr/bin/env python3
"""T3 self-check: the local-grade adapter (verdict -> resolved.json + manifest status) and the
empty-patch short-circuit. Pure logic, no docker. Run: python3 tests/test_grading_adapter.py"""
import csv
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import execute_bench as eb  # noqa: E402


def test_finalize_maps_verdicts():
    d = tempfile.mkdtemp()
    manifest = os.path.join(d, "manifest.csv")
    rows = [["task", "harness", "model", "prompt", "run", "outdir", "start", "end", "duration_s", "status"],
            ["abs-x", "opencode", "modal/GLM", "instr", "1", f"{d}/r1", "0", "1", "1", "n/a"],
            ["abs-x", "claude", "anthropic/claude-opus-4-8", "instr", "1", f"{d}/r2", "0", "1", "1", "n/a"],
            ["abs-x", "opencode", "modal/GLM", "instr", "2", f"{d}/r3", "0", "1", "1", "n/a"],
            ["abs-x", "opencode", "modal/GLM", "instr", "3", f"{d}/r4", "0", "1", "1", "errored"]]
    with open(manifest, "w", newline="") as f:
        csv.writer(f).writerows(rows)
    grades = {
        f"{d}/r1": {"task": "abs-x", "harness": "opencode", "model": "modal/GLM", "pv": "instr", "run": "1", "verdict": "resolved"},
        f"{d}/r2": {"task": "abs-x", "harness": "claude", "model": "anthropic/claude-opus-4-8", "pv": "instr", "run": "1", "verdict": "unresolved"},
        f"{d}/r3": {"task": "abs-x", "harness": "opencode", "model": "modal/GLM", "pv": "instr", "run": "2", "verdict": "errored"},
    }
    eb.finalize_grades(d, grades)

    got = {r[5]: r[9] for r in list(csv.reader(open(manifest)))[1:]}
    assert got[f"{d}/r1"] == "pass", got            # resolved -> pass
    assert got[f"{d}/r2"] == "fail", got            # unresolved -> fail
    assert got[f"{d}/r3"] == "errored", got         # verifier infra-fail -> excluded
    assert got[f"{d}/r4"] == "errored", got         # generation-errored row never overridden

    resolved = json.load(open(os.path.join(d, "resolved.json")))
    # errored verdict is NOT in resolved.json (excluded from resolve-rate + cost)
    assert len(resolved) == 2, resolved
    k = "abs-x::opencode__modal_GLM__instr__run1"   # matches aggregate._pred_key derivation
    assert resolved[k] == {"instance_id": "abs-x", "model_name_or_path": "opencode__modal_GLM__instr__run1",
                           "resolved": True}, resolved[k]
    kc = "abs-x::claude__anthropic_claude-opus-4-8__instr__run1"
    assert resolved[kc]["resolved"] is False, resolved


def test_empty_patch_is_unresolved():
    d = tempfile.mkdtemp()
    # no model.patch at all
    assert eb.grade_run("abs-x", d, {"docker_image": "x"}, d, {"runid": "t"}) == "unresolved"
    # empty model.patch
    open(os.path.join(d, "model.patch"), "w").close()
    assert eb.grade_run("abs-x", d, {"docker_image": "x"}, d, {"runid": "t"}) == "unresolved"


if __name__ == "__main__":
    test_finalize_maps_verdicts()
    test_empty_patch_is_unresolved()
    print("ok — grading adapter maps verdicts + short-circuits empty patches")
