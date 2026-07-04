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


def judge_run(model, task, status, tscript, dff):
    prompt = f"""You're reviewing how a coding agent handled a task. Be short, casual and honest —
no hype, no marketing words, no exaggeration. Call out real strengths AND weaknesses plainly.

Task: {task}
Outcome: {"passed the tests" if status == "pass" else "did NOT pass the tests"}

Transcript (what it did):
{tscript[:5000] or "(none)"}

Its code change (diff):
{dff or "(not available)"}

Reply ONLY with compact JSON:
{{"approach":"how it went about it, 1 sentence","efficiency":"steps/backtracks/verbosity, 1 sentence","code_quality":"1 sentence","tldr":"one casual honest sentence"}}"""
    try:
        r = subprocess.run(["opencode", "run", prompt, "-m", model, "--format", "json"],
                           capture_output=True, text=True, timeout=300)
    except Exception as e:
        return {"tldr": f"(judge error: {type(e).__name__})"}
    text = opencode_text(r.stdout)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    err = (text or r.stderr or r.stdout or "").strip().replace("\n", " ")
    return {"tldr": f"(no verdict — {err[:200] or 'empty judge output'})"}


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
        key = (h, m)
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
         "Same tasks and prompts, so this reflects **agent behavior** (model + harness). Per task, averaged:",
         "",
         "| harness | model | steps/task | tool calls/task | output tokens/task |",
         "|---|---|---|---|---|"]
    per = {}
    for (h, m), a in agg.items():
        n = max(a["runs"], 1)
        per[(h, m)] = (a["steps"] / n, a["tools"] / n, a["out"] / n)
        L.append(f"| {h} | {aggregate.model_id(m)} | {per[(h, m)][0]:.0f} | "
                 f"{per[(h, m)][1]:.0f} | {per[(h, m)][2]:.0f} |")
    L.append("")
    gpu = next((k for k in agg if aggregate.is_self_hosted(k[1])), None)
    if gpu:
        for k in agg:
            if k != gpu and per[k][1] and per[k][2]:
                L.append(f"- vs **{k[0]}:{aggregate.model_id(k[1])}**, the self-hosted run does "
                         f"~{per[gpu][1] / per[k][1]:.1f}× the tool calls and ~{per[gpu][2] / per[k][2]:.1f}× "
                         "the output tokens for the same fixes — more work per task, which compounds its GPU cost.")
        L.append("")
    return L


def timeline(summary):
    """Per-task start/finish + per-model active/overlap, from results_detailed.csv + summary."""
    L = ["## Timeline (when each task ran)\n"]
    for r in summary:
        a, o = r.get("active_s", ""), r.get("overlap_s", "")
        if a != "" or o != "":
            L.append(f"- **{r['model']}** — {a}s active wall-clock, {o}s saved by parallel overlap")
    L.append("")
    det = os.path.join(RESULTS_DIR, "results_detailed.csv")
    if os.path.exists(det):
        rows = list(csv.DictReader(open(det)))
        cols = [c for c in ("task", "model", "run", "start", "end", "duration_s", "status")
                if rows and c in rows[0]]
        L.append("| " + " | ".join(cols) + " |")
        L.append("|" + "|".join(["---"] * len(cols)) + "|")
        for r in rows:
            L.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
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
    try:
        call, up, passes = float(gpu["call_s"]), float(gpu["active_s"]), int(gpu["passes"])
        idle = max(0.0, up - call)
        up_task = up / 3600 * rate / passes
        call_task = call / 3600 * rate / passes
        idle_task = idle / 3600 * rate / passes
        pct = 100 * call / up if up else 0
        L += [f"> **⚠️ Attribution caveat — this is a choice, not a real saving.** Modal bills the "
              f"container's **uptime**, not compute-seconds, so you can't *deduct* idle from the bill — "
              f"a sole tenant pays all {up:.0f}s of wall-clock (**~${up_task:.2f}/task**). What we can do "
              "is **decompose** it:",
              ">",
              f"> &nbsp;&nbsp;&nbsp;&nbsp;`sole-tenant ${up_task:.2f}/task  =  generation ${call_task:.2f} "
              f"({pct:.0f}%)  +  idle tax ${idle_task:.2f} ({100 - pct:.0f}%)`",
              ">",
              f"> We headline the **${call_task:.2f} generation floor** (the fair shared-endpoint number). "
              f"The **${idle_task:.2f}/task idle tax** isn't the model's fault and isn't a saving you can "
              "book alone — it's under-utilization, recovered only by packing the endpoint with concurrent "
              "work.", ""]
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
         "**Empirical (0-10, relative)** = observed effort pooled across all models (steps, tool calls, "
         "output tokens, duration), normalized within this task set. **LLM difficulty (1-5)** is an "
         "independent blind rating of the task instruction itself. `pass_rate` is the outcome across all runs.",
         "",
         "| task | empirical 0-10 | LLM 1-5 | pass_rate | avg_steps | avg_out_tok |",
         "|---|---|---|---|---|---|"]
    for r in rows:
        d = difficulty.get(r["task"], {})
        L.append(f"| {r['task']} | {r['complexity']} | {d.get('difficulty', '—')} | "
                 f"{r['pass_rate']} | {r['avg_steps']} | {r['avg_out_tok']} |")
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
        sys.exit("no results/manifest.csv — run run_bench.sh first")

    rows = list(csv.DictReader(open(manifest)))
    verdicts = {}  # (harness, model) -> list of (task, status, verdict)
    for i, row in enumerate(rows, 1):
        m, task, outdir, status = row["model"], row["task"], row.get("outdir", ""), row["status"]
        h = row.get("harness", "opencode")
        sys.stderr.write(f"[{i}/{len(rows)}] judging {task} | {h} | {m}\n")
        v = judge_run(model, task, status, blind(transcript(outdir)), blind(diff(outdir)))
        verdicts.setdefault(f"{h} · {m}", []).append((task, status, v))

    # blind LLM difficulty rating, once per unique task (from its prompt)
    difficulty = {}
    tasks_seen = []
    for row in rows:
        if row["task"] not in tasks_seen:
            tasks_seen.append(row["task"])
    for i, t in enumerate(tasks_seen, 1):
        pf = os.path.join("tasks", t, "prompt.txt")
        prompt = open(pf).read() if os.path.exists(pf) else ""
        sys.stderr.write(f"[difficulty {i}/{len(tasks_seen)}] {t}\n")
        difficulty[t] = rate_difficulty(model, t, blind(prompt))

    # build report: numbers (summary.csv) + qualitative notes
    lines = ["# Benchmark report\n", f"_Judge: {model} (blinded review)_\n"]
    summ = os.path.join(RESULTS_DIR, "summary.csv")
    if os.path.exists(summ):
        s = list(csv.DictReader(open(summ)))
        cols = ["harness", "model", "passes", "runs", "success_rate", "avg_tokens_in",
                "avg_tokens_out", "avg_duration_s", "call_s", "active_s", "idle_s",
                "overlap_s", "cost_per_successful_task", "cost_basis"]
        cols = [c for c in cols if c in s[0]]
        lines.append("## Numbers\n")
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "|".join(["---"] * len(cols)) + "|")
        for r in s:
            lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
        lines.append("")
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
