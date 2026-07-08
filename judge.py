#!/usr/bin/env python3
"""LLM-judge the agents' work (transcript + diff) and merge with the numbers into a report.

    python3 judge.py --judge gemini        # openai | gemini | anthropic | glm
    python3 judge.py --model google/gemini-3-flash-preview   # or any opencode model ref

Reads results/manifest.csv, and per run pulls the agent transcript (from output.log) and its
code diff (git diff HEAD in final_repo). Both are BLINDED (model/provider names stripped) so
the judge doesn't play favorites. The judge is asked for short, casual, honest notes — no hype.
Output: results/report.md — the numbers (from summary.csv) + the qualitative notes per model.

Pick a judge that ISN'T in the comparison when you can (avoids self-preference).
"""
import csv
import json
import os
import re
import subprocess
import sys

import aggregate   # reuse harness_disp / model_disp / parsers so labels can't drift


def load_env(path=".env"):
    """Load .env into os.environ (so `opencode run` sees the API keys), and drop the
    ANTHROPIC/OPENAI/GOOGLE base-url overrides that break the default endpoints."""
    for var in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "GOOGLE_BASE_URL"):
        os.environ.pop(var, None)
    os.environ["NO_COLOR"] = "1"
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line[7:].strip() if line.startswith("export ") else line
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
RESULTS_DIR = os.environ.get("RESULTS_DIR", "./results")
JUDGES = {
    "openai": "openai/gpt-5.1",
    "gemini": "google/gemini-3-flash-preview",
    "anthropic": "anthropic/claude-opus-4-8",
    "glm": "modal/zai-org/GLM-5.2-FP8",
}
BLIND = ["claude", "anthropic", "openai", "gpt", "gemini", "google", "glm", "zai-org",
         "modal", "opus", "sonnet", "haiku", "fable", "codex", "big-pickle"]


def blind(s):
    for w in BLIND:
        s = re.sub(re.escape(w), "the-model", s, flags=re.IGNORECASE)
    return s


def transcript(outdir):
    """Readable transcript from output.log — handles both opencode JSON-lines and Claude Code
    stream-json: agent text + tool calls, either format."""
    out = []
    try:
        for line in open(os.path.join(outdir, "output.log")):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            t = ev.get("type") if isinstance(ev, dict) else None
            if t == "text":                                    # opencode
                txt = ((ev.get("part", {}) or {}).get("text") or "").strip()
                if txt:
                    out.append(f"· {txt}")
            elif t == "tool_use":                              # opencode
                part = ev.get("part", {}) or {}
                inp = (part.get("state", {}) or {}).get("input", {})
                out.append(f"  [{part.get('tool', '?')}] {json.dumps(inp)[:200]}")
            elif t == "assistant":                             # Claude Code stream-json
                for b in ev.get("message", {}).get("content", []) or []:
                    if b.get("type") == "text":
                        txt = (b.get("text") or "").strip()
                        if txt:
                            out.append(f"· {txt}")
                    elif b.get("type") == "tool_use":
                        out.append(f"  [{b.get('name', '?')}] {json.dumps(b.get('input', {}))[:200]}")
    except Exception:
        return ""
    return "\n".join(out)


def diff(outdir):
    repo = os.path.join(outdir, "final_repo")
    if not os.path.isdir(os.path.join(repo, ".git")):
        return ""
    try:
        r = subprocess.run(["git", "-C", repo, "diff", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout[:6000]
    except Exception:
        return ""


def opencode_text(stdout):
    """Concatenate the assistant text parts from `opencode run --format json` output."""
    parts = []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "text":
            parts.append(ev.get("part", {}).get("text", ""))
    return "".join(parts).strip()


def judge_all(model, items):
    """ONE judge call for ALL runs (instead of one call per run). Sends a blinded, trimmed batch and
    asks for a JSON object mapping each run's number -> {approach, efficiency, code_quality, tldr}.
    Trades some per-run context depth for a single round-trip. Returns {index(int): verdict dict},
    with every index filled (a placeholder for any the judge omitted). `items` carry task/status and
    the already-blinded transcript+diff."""
    if not items:
        return {}
    # adaptive per-run char budget: a small batch gets rich context, a big one still fits one call
    budget = max(500, 90000 // len(items))
    blocks = []
    for i, it in enumerate(items):
        blocks.append(
            f"=== RUN {i} ===\n"
            f"Task: {it['task']}\n"
            f"Outcome: {'passed the tests' if it['status'] == 'pass' else 'did NOT pass the tests'}\n"
            f"Transcript:\n{it['tscript'][:budget] or '(none)'}\n"
            f"Diff:\n{it['diff'][:budget] or '(not available)'}\n"
        )
    prompt = (
        "You're reviewing how coding agents handled several tasks. Be short, casual and honest — "
        "no hype, no marketing words, no exaggeration. Judge EACH run independently; call out real "
        "strengths AND weaknesses plainly.\n\n"
        f"There are {len(items)} runs below, numbered RUN 0 .. RUN {len(items) - 1}.\n\n"
        + "\n".join(blocks)
        + "\n\nReply ONLY with a compact JSON object mapping each run number (as a string key) to its "
        'review, e.g. {"0": {"approach":"how it went about it","efficiency":"steps/backtracks/verbosity"'
        ',"code_quality":"...","tldr":"one casual honest sentence"}, "1": {...}}. '
        "Include every run number 0.." + str(len(items) - 1) + ". One short sentence per field."
    )
    try:
        r = subprocess.run(["opencode", "run", prompt, "-m", model, "--format", "json"],
                           capture_output=True, text=True, timeout=900)
    except Exception as e:
        return {i: {"tldr": f"(judge error: {type(e).__name__})"} for i in range(len(items))}
    m = re.search(r"\{.*\}", opencode_text(r.stdout), re.DOTALL)
    out = {}
    if m:
        try:
            for k, v in json.loads(m.group(0)).items():
                try:
                    out[int(k)] = v
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass
    for i in range(len(items)):        # never drop a run from the report
        out.setdefault(i, {"tldr": "(no verdict returned for this run)"})
    return out


def cost_model(summary):
    """Closing the gap: how concurrency turns the sole-tenant uptime cost into the call-time floor,
    and how that floor compares to Claude. All from measured summary numbers."""
    rate = float(os.environ.get("GLM_GPU_HOURLY_USD", "50.7"))
    gpu = next((r for r in summary if str(r.get("cost_basis", "")).startswith("gpu")), None)
    api = next((r for r in summary if r.get("cost_basis") == "api_ccusage"), None)
    try:
        claude = float(api["cost_per_successful_task"])
        call, up, passes = float(gpu["call_s"]), float(gpu["active_s"]), int(gpu["passes"])
    except (TypeError, ValueError, KeyError):
        return []
    floor = call / 3600 * rate / passes           # fully-packed: only generation billed
    sole = up / 3600 * rate / passes              # sole tenant: full uptime billed
    conc = up / call if call else 0               # concurrent bursty sessions to keep GPU saturated
    verdict = ("competitive with" if floor <= claude * 1.25
               else "closer, but still above" if floor <= claude * 2 else "still well above")
    L = ["## How many parallel sessions to beat Claude\n",
         f"GLM only touches the GPU **~{100 * call / up:.0f}%** of each session (the rest is local "
         f"pip/pytest/git). So one sole-tenant session wastes most of the rented GPU:",
         "",
         f"- Claude: **${claude:.2f}/task**.",
         f"- GLM, 1 session (sole tenant, full uptime billed): **~${sole:.2f}/task**.",
         f"- GLM, fully packed (call-time floor): **~${floor:.2f}/task**.",
         "",
         f"The lever is **concurrency**: run enough bursty sessions that they fill each other's script "
         f"gaps and keep the endpoint generating. Roughly **uptime ÷ call-time ≈ {conc:.1f} concurrent "
         f"sessions** keep the GPU saturated — past that you pay near the floor, below it you pay for idle.",
         "",
         f"**Bottom line:** even fully packed, GLM's floor (${floor:.2f}/task) is **{verdict}** Claude's "
         f"${claude:.2f}. Getting there needs ~{max(1, round(conc))}+ concurrent sessions *and* sustained "
         "volume to keep them coming; bursty or low-volume use pays the sole-tenant ${:.2f} and loses to "
         "the API.".format(sole),
         "",
         "*(call-time is measured at low batch; at higher concurrency the 8×B200 batches requests, so the "
         "true packed floor is lower still — a load test would pin it down.)*", ""]
    return L


def efficiency(rows):
    """Steps / tool-calls / output tokens per task, per (harness, model), from output.log (both
    opencode and Claude Code formats). Same tasks/prompts, so it reflects agent behavior: fewer
    turns to the fix = fewer output tokens = less generation time = less GPU cost. Reuses the
    aggregate.py parsers so it can't drift from the cost numbers. `rows` = manifest DictReader rows."""
    import aggregate
    agg = {}
    for r in rows:
        m = r["model"]
        h = r.get("harness") or aggregate.harness_of(m)
        pv = r.get("prompt", "v1")
        key = (h, m, pv)
        a = agg.setdefault(key, {"runs": 0, "steps": 0, "tools": 0, "out": 0})
        a["runs"] += 1
        st = aggregate.claude_stats(r.get("outdir", "")) if h == "claude" \
            else aggregate.log_stats(r.get("outdir", ""))
        a["steps"] += st["steps"]
        a["tools"] += st["tools"]
        a["out"] += st["out"]
    if not agg:
        return []
    L = ["## Efficiency — how much work each run did\n",
         "Same tasks, so this reflects **agent behavior** (model + harness + prompt version). Per task, averaged:",
         "",
         "| harness | model | prompt | steps/task | tool calls/task | output tokens/task |",
         "|---|---|---|---|---|---|"]
    per = {}
    for (h, m, pv), a in agg.items():
        n = max(a["runs"], 1)
        per[(h, m, pv)] = (a["steps"] / n, a["tools"] / n, a["out"] / n)
        L.append(f"| {aggregate.harness_disp(h)} | {aggregate.model_disp(m)} | {pv} | {per[(h, m, pv)][0]:.0f} | "
                 f"{per[(h, m, pv)][1]:.0f} | {per[(h, m, pv)][2]:.0f} |")
    L.append("")
    gpu = next((k for k in agg if aggregate.is_self_hosted(k[1])), None)
    if gpu:
        for k in agg:
            if k != gpu and per[k][1] and per[k][2]:
                L.append(f"- vs **{aggregate.harness_disp(k[0])}:{aggregate.model_disp(k[1])} (prompt {k[2]})**, the "
                         f"self-hosted run does ~{per[gpu][1] / per[k][1]:.1f}× the tool calls and "
                         f"~{per[gpu][2] / per[k][2]:.1f}× the output tokens for the same fixes — more work per "
                         "task, which compounds its GPU cost.")
        L.append("")
    return L


def timeline(summary):
    """Per-model wall-clock + parallelism factor, then per-task start/finish from results_detailed."""
    L = ["## Timeline (when each task ran)\n"]
    for r in summary:
        a = aggregate._f(r.get("active_s"))
        if not a:
            continue
        o = aggregate._f(r.get("overlap_s")) or 0.0    # overlap = sum(durations) - union(active)
        par = (a + o) / a                        # avg parallelism: total work / wall-clock
        pv = r.get("prompt", "")
        L.append(f"- **{aggregate.harness_disp(r['harness'])}:{aggregate.model_disp(r['model'])}"
                 f"{' · prompt ' + pv if pv else ''}** — "
                 f"{a:.0f}s wall-clock for {r.get('runs', '?')} runs (~{par:.1f}× parallel)")
    L.append("")
    det = os.path.join(RESULTS_DIR, "results_detailed.csv")
    if os.path.exists(det):
        rows = list(csv.DictReader(open(det)))
        cols = [c for c in ("task", "model", "prompt", "run", "start", "end", "duration_s", "status")
                if rows and c in rows[0]]
        L.append("| " + " | ".join(cols) + " |")
        L.append("|" + "|".join(["---"] * len(cols)) + "|")
        for r in rows:
            L.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
        L.append("")
    return L


def cost_table(summary):
    """Per-arm cost two ways so the reader sees the range and the concurrency lever:
    sole-tenant (one task owns the 8×B200) vs packed (endpoint filled with concurrent tasks)."""
    def packed(r):
        if aggregate.is_self_hosted(r["model"]):
            w, p = aggregate._f(r.get("gpu_wall_cost_usd")), aggregate._f(r.get("passes"))
            return w / p if (w and p) else None
        return aggregate._f(r.get("cost_per_successful_task"))   # API is per-token: sole == packed

    def usd(v):
        return f"${v:.3f}" if v is not None else "—"

    L = ["## Cost per completed task — sole-tenant vs packed\n",
         "For GLM the two numbers bracket reality. **sole** = one task alone on the rented 8×B200 — its "
         "own generation time × rate (you pay for all 8 GPUs to serve a single task). **packed** = the "
         "endpoint filled with concurrent tasks — union of generation ÷ tasks. The gap between them is "
         "the **concurrency lever**: packing the endpoint is what makes GLM cheap. Both count generation "
         f"only (step_start→step_finish × ~${float(os.environ.get('GLM_GPU_HOURLY_USD','50.7')):.0f}/hr, "
         "no local scripts). Claude/GPT are per-token — concurrency-invariant — so sole == packed.",
         "",
         "| harness | model | prompt | success | $/task sole | $/task packed |",
         "|---|---|---|---|---|---|"]
    for r in summary:
        cst = aggregate._f(r.get("cost_per_successful_task"))
        L.append(f"| {aggregate.harness_disp(r['harness'])} | {aggregate.model_disp(r['model'])} "
                 f"| {r.get('prompt', '')} | {r.get('passes', '')}/{r.get('runs', '')} "
                 f"| {usd(cst)} | {usd(packed(r))} |")
    L.append("")
    return L


def cost_analysis(summary):
    """Narrative on WHY the two costs differ — generated from summary.csv so it can't drift."""
    rate = float(os.environ.get("GLM_GPU_HOURLY_USD", "50.7"))
    gpu = next((r for r in summary if str(r.get("cost_basis", "")).startswith("gpu")), None)
    api = next((r for r in summary if r.get("cost_basis") == "api_ccusage"), None)
    L = ["## Cost — what's really going on\n",
         "Two different meters — read the `cost_basis` column:",
         "",
         "- **api_ccusage** (Claude): real billed dollars from ccusage — tokens × price, with "
         "prompt-caching applied (cached input ~10× cheaper). Pay per token, $0 while idle.",
         f"- **gpu_calls** (GLM): charged on **endpoint call time only** — the seconds the agent "
         f"actually spent generating on the endpoint (`call_s`), × the ~${rate:.0f}/hr rate. Local "
         "script time (pip, pytest, git, file I/O) is **excluded**, since the GPU sits idle then.",
         ""]
    # Endpoint idle is a SHARED property of the GLM endpoint, computed ONCE across all modal arms —
    # not charged per arm (that double-blames the low-reasoning arm for the endpoint just being up).
    try:
        gpu_rows = [r for r in summary if str(r.get("cost_basis", "")).startswith("gpu")]
        up = sum(float(r["active_s"]) for r in gpu_rows if r.get("active_s"))
        gen = sum(float(r["gen_s"]) for r in gpu_rows if r.get("gen_s"))   # generation wall-clock (concurrency-safe)
        idle = max(0.0, up - gen)
        L += [f"> **⚠️ Endpoint idle (charged once, not per-arm).** Modal bills the container's "
              f"**uptime**, not compute-seconds. Across all GLM arms the endpoint was up {up:.0f}s but "
              f"generated only {gen:.0f}s → **{idle:.0f}s idle = ~${idle / 3600 * rate:.2f}** of "
              "under-utilization (agents spend most of each task in local pip/pytest/git). That idle is "
              "a property of *keeping the endpoint up*, recoverable only by packing it with concurrent "
              "work — so we report it once here and charge each arm's `$/task` on **generation only**. "
              "Blaming the reasoning-off arm for idle while it barely touches the GPU would be backwards.",
              ""]
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        pass
    L += ["**Why the two meters look so different — structural, not a modelling error:**",
          "",
          "| | GLM self-host | Claude API |",
          "|---|---|---|",
          "| hardware | you rent 8×B200 (~$50/hr) whole | GPU shared across thousands of users |",
          "| you pay for | **uptime** — idle included | only tokens generated |",
          "| a bursty agent session | GPU idle most of it (pip/pytest/git) | irrelevant |",
          "| caching | barely used | 80k+ tokens cached at ~10× discount |",
          ""]
    return L


def rate_difficulty(judge_model, task, prompt):
    """Independent, blind LLM rating of how hard the TASK is (1-5), from its instruction alone —
    not contaminated by which model ran it or how it went."""
    q = f"""Rate how hard this coding task is for an AI agent, on a 1-5 scale (1 = trivial, 5 = very hard).
Consider scope, ambiguity, debugging/reasoning required, and codebase breadth. Judge the TASK itself,
not any particular solution.

Task: {task}
Instruction given to the agent:
{prompt[:3000] or "(prompt unavailable)"}

Reply ONLY with compact JSON: {{"difficulty": <integer 1-5>, "why": "one short reason"}}"""
    try:
        r = subprocess.run(["opencode", "run", q, "-m", judge_model, "--format", "json"],
                           capture_output=True, text=True, timeout=180)
    except Exception:
        return {}
    m = re.search(r"\{.*\}", opencode_text(r.stdout), re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def complexity_section(difficulty):
    """Merge the empirical complexity (aggregate's complexity.csv) with the blind LLM difficulty."""
    path = os.path.join(RESULTS_DIR, "complexity.csv")
    if not os.path.exists(path):
        return []
    rows = list(csv.DictReader(open(path)))
    L = ["## Task complexity\n",
         "Per **(task, prompt version)** — `v1` (terse/raw issue) usually demands more than `v2` (shaped). "
         "**Source**: `swe-bench` = a real SWE-bench Verified issue (`v1` = the problem statement verbatim, "
         "`v2` = it wrapped in our uniform template); `invented` = a task + prompt we wrote. **Empirical "
         "(0-10, relative)** = observed effort pooled across all models for that pair (steps, tool calls, "
         "output tokens, duration). **LLM difficulty (1-5)** is an independent blind rating of that exact "
         "prompt. `pass_rate` is the outcome across all runs of the pair.",
         "",
         "| task | prompt | source | empirical 0-10 | LLM 1-5 | pass_rate | avg_steps | avg_out_tok |",
         "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        d = difficulty.get((r["task"], r.get("prompt", "v1")), {})
        L.append(f"| {r['task']} | {r.get('prompt', '')} | {r.get('source', '')} | {r['complexity']} | "
                 f"{d.get('difficulty', '—')} | {r['pass_rate']} | {r['avg_steps']} | {r['avg_out_tok']} |")
    L.append("")
    return L


def main():
    args = sys.argv[1:]
    model = None
    if "--model" in args:
        model = args[args.index("--model") + 1]
    elif "--judge" in args:
        model = JUDGES.get(args[args.index("--judge") + 1])
    if not model:
        sys.exit("usage: judge.py --judge openai|gemini|anthropic|glm   (or --model <ref>)")

    manifest = os.path.join(RESULTS_DIR, "manifest.csv")
    if not os.path.exists(manifest):
        sys.exit("no results/manifest.csv — run bench.sh first")

    rows = list(csv.DictReader(open(manifest)))
    # Judge ONE representative run per (harness, model, PROMPT, task): the note is the same across
    # repeat runs (repeats are for success-rate stability), but a different prompt version produces a
    # different transcript+diff, so each prompt version is judged separately. Prefer a passing run.
    rep = {}
    for r in rows:
        k = (r.get("harness", "opencode"), r["model"], r.get("prompt", "v1"), r["task"])
        if k not in rep or (r["status"] == "pass" and rep[k]["status"] != "pass"):
            rep[k] = r
    judge_rows = list(rep.values())
    # ONE call: build the blinded batch, judge all runs at once, then map verdicts back by index.
    items = [{"h": r.get("harness", "opencode"), "m": r["model"], "pv": r.get("prompt", "v1"),
              "task": r["task"], "status": r["status"],
              "tscript": blind(transcript(r.get("outdir", ""))), "diff": blind(diff(r.get("outdir", "")))}
             for r in judge_rows]
    sys.stderr.write(f"judging {len(items)} runs in a single call to {model}\n")
    notes = judge_all(model, items)
    verdicts = {}  # "harness · model · prompt" -> list of (task, status, verdict)
    for idx, it in enumerate(items):
        verdicts.setdefault(f"{it['h']} · {it['m']} · prompt {it['pv']}", []).append(
            (it["task"], it["status"], notes.get(idx, {"tldr": "(no verdict)"})))

    # blind LLM difficulty rating, per unique (task, prompt version) — v1 (raw/terse) and v2 (shaped)
    # are genuinely different difficulties, so each is rated from its own prompt file.
    def _prompt_path(task, pv):
        p = os.path.join("tasks", task, f"prompt.{pv}.txt")
        return p if os.path.exists(p) else ""
    difficulty = {}
    pairs = []
    for row in rows:
        key = (row["task"], row.get("prompt", "v1"))
        if key not in pairs:
            pairs.append(key)
    for i, (t, pv) in enumerate(pairs, 1):
        pf = _prompt_path(t, pv)
        prompt = open(pf).read() if pf else ""
        sys.stderr.write(f"[difficulty {i}/{len(pairs)}] {t} | prompt {pv}\n")
        difficulty[(t, pv)] = rate_difficulty(model, t, blind(prompt))

    # build report: numbers (summary.csv) + qualitative notes
    lines = ["# Benchmark report\n", f"_Judge: {model} (blinded review)_\n"]
    summ = os.path.join(RESULTS_DIR, "summary.csv")
    if os.path.exists(summ):
        s = list(csv.DictReader(open(summ)))
        cols = ["harness", "model", "prompt", "passes", "runs", "success_rate", "avg_tokens_in",
                "avg_tokens_out", "avg_duration_s", "call_s", "gen_s", "active_s", "idle_s",
                "overlap_s", "cost_per_successful_task", "gpu_wall_cost_usd", "cost_basis"]
        cols = [c for c in cols if c in s[0]]
        def _cell(c, r):
            if c == "harness":
                return aggregate.harness_disp(r.get(c, ""))
            if c == "model":
                return aggregate.model_disp(r.get(c, ""))   # keep provider: modal vs modal-nothink
            return str(r.get(c, ""))
        lines.append("## Numbers\n")
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "|".join(["---"] * len(cols)) + "|")
        for r in s:
            lines.append("| " + " | ".join(_cell(c, r) for c in cols) + " |")
        lines.append("")
        lines += cost_table(s)
        lines += timeline(s)
        lines += cost_analysis(s)
        lines += cost_model(s)
        lines += efficiency(rows)
        lines += complexity_section(difficulty)

    lines.append("## How each model worked (blinded judge notes)\n")
    for m, items in verdicts.items():
        lines.append(f"### {m}")
        for task, status, v in items:
            mark = "✅" if status == "pass" else "❌"
            tldr = v.get("tldr", "")
            lines.append(f"- {mark} **{task}** — {tldr}")
            for k in ("approach", "efficiency", "code_quality"):
                if v.get(k):
                    lines.append(f"    - {k}: {v[k]}")
        lines.append("")

    report = os.path.join(RESULTS_DIR, "report.md")
    open(report, "w").write("\n".join(lines) + "\n")
    print(f"wrote {report}")


if __name__ == "__main__":
    main()
