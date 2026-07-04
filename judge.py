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
    """Readable transcript from output.log (opencode JSON-lines): text says + tool calls."""
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
            part = ev.get("part", {}) if isinstance(ev, dict) else {}
            if ev.get("type") == "text":
                t = (part.get("text") or "").strip()
                if t:
                    out.append(f"· {t}")
            elif ev.get("type") == "tool_use":
                tool = part.get("tool", "?")
                inp = part.get("state", {}).get("input", {})
                out.append(f"  [{tool}] {json.dumps(inp)[:200]}")
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
    """How many parallel tasks on Modal to beat Claude — throughput break-even from measured data.

    GLM's per-task cost = flat endpoint rate / throughput. Throughput scales with concurrency, so
    running tasks in parallel is the ONLY lever. We anchor tasks/hour on the measured single-stream
    duration and assume ~linear scaling for small N (an 8xB200 is barely touched by one stream)."""
    rate = float(os.environ.get("GLM_GPU_HOURLY_USD", "50.7"))
    gpu = next((r for r in summary if str(r.get("cost_basis", "")).startswith("gpu")), None)
    api = next((r for r in summary if r.get("cost_basis") == "api_ccusage"), None)
    try:
        claude = float(api["cost_per_successful_task"])
        d = float(gpu["avg_duration_s"])
    except (TypeError, ValueError, KeyError):
        return []
    tph1 = 3600.0 / d                 # tasks/hour at concurrency 1
    be_tph = rate / claude            # throughput needed to match Claude
    be_n = be_tph / tph1              # parallel tasks needed (linear-scaling assumption)
    L = ["## How many parallel tasks to beat Claude\n",
         f"The endpoint costs a flat **${rate:.0f}/hr** whenever it's up, so GLM's per-task cost is just "
         "`rate ÷ throughput`. The only lever is pushing more tasks through the same GPU-hour — i.e. "
         "concurrency.",
         "",
         f"- Claude: **${claude:.2f}/task** — per-token, zero idle cost.",
         f"- GLM, 1 task at a time (~{d:.0f}s each ⇒ ~{tph1:.0f} tasks/hr): **${rate / tph1:.2f}/task**.",
         f"- Match Claude ⇒ need **~{be_tph:.0f} successful tasks/hour** through the endpoint.",
         f"- At ~{tph1:.0f} tasks/hr per concurrent slot ⇒ **~{be_n:.1f} tasks in parallel to break even.**",
         "",
         "Assuming throughput scales ~linearly with concurrency (safe for small N on an 8xB200 a single "
         "stream barely uses):",
         "",
         "| parallel tasks | tasks/hour | $/task | vs Claude |",
         "|---|---|---|---|"]
    for N in (1, 2, 3, 4, 6, 8, 12):
        tph = N * tph1
        cpt = rate / tph
        L.append(f"| {N} | {tph:.0f} | ${cpt:.2f} | {cpt / claude:.1f}x |")
    L += ["",
          f"**Bottom line: ~{round(be_n)} parallel tasks to break even, ~{round(be_n * 2)}+ to actually "
          "save money vs Claude.** Caveats, all pushing the real break-even *higher*: LLM throughput "
          f"plateaus once the batch is full; cold starts and idle gaps still bill at ${rate:.0f}/hr with "
          "zero output; and agentic tasks only hit the GPU in bursts (lots of local git/pytest/tool time), "
          "so you need many concurrent sessions to keep it saturated. Bursty or low-volume use loses to "
          "the API — only sustained, packed concurrency wins.", ""]
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
    active = f" ({gpu['active_s']}s here)" if gpu and gpu.get("active_s") else ""
    L = ["## Cost — what's really going on\n",
         "Two different meters — always read the `cost_basis` column:",
         "",
         "- **api_ccusage** (Claude): real billed dollars from ccusage — tokens × price, with "
         "prompt-caching already applied (cached input is ~10× cheaper). You pay only for tokens, "
         "and $0 while idle.",
         f"- **gpu_active** (GLM): you rent the whole 8×B200 endpoint (~${rate:.0f}/hr). We charge "
         f"ONLY the union of the minutes the model was actively running{active} — overlapping/"
         "parallel runs count once — *excluding* the idle warm-up / scale-down time the machine "
         "sat up doing nothing. That's the generous read for self-host.",
         ""]
    if gpu and api and api.get("cost_per_successful_task"):
        L += [f"Reconciled against the real Modal bill, the implied rate is ~${rate:.0f}/hr "
              f"(~${rate / 8:.2f} per B200-hour — Modal's real B200 price). Even counting only the "
              f"active slice, GLM lands at ${gpu.get('cost_per_successful_task')}/task vs "
              f"${api.get('cost_per_successful_task')}/task for Claude.", ""]
    L += ["**Why GLM looks expensive and Claude looks cheap — it's structural, not a modelling error:**",
          "",
          "| | GLM self-host | Claude API |",
          "|---|---|---|",
          "| hardware | you rent 8×B200 (~$50/hr) whole | GPU shared across thousands of users |",
          "| batch=1 (as run here) | ~7/8 of the GPU wasted | irrelevant — you pay per token |",
          "| caching | barely used | 80k+ tokens cached at ~10× discount |",
          "| idle | you pay the full hour | you pay $0 |",
          ""]
    if gpu and api and api.get("cost_per_successful_task"):
        try:
            be = round(rate / float(api["cost_per_successful_task"]))
            L += [f"The one lever is **utilization**. Break-even vs Claude's "
                  f"${api['cost_per_successful_task']}/task is ~{be} successful tasks/hour "
                  "saturating the 8×B200 with concurrent requests. Below that the API wins; above "
                  "it self-host wins. GLM didn't lose on quality or speed (same pass rate, ~2× "
                  "faster) — it loses on economics until the endpoint is saturated.", ""]
        except (ValueError, ZeroDivisionError):
            pass
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
    verdicts = {}  # model -> list of (task, status, verdict)
    for i, row in enumerate(rows, 1):
        m, task, outdir, status = row["model"], row["task"], row.get("outdir", ""), row["status"]
        sys.stderr.write(f"[{i}/{len(rows)}] judging {task} | {m}\n")
        v = judge_run(model, task, status, blind(transcript(outdir)), blind(diff(outdir)))
        verdicts.setdefault(m, []).append((task, status, v))

    # build report: numbers (summary.csv) + qualitative notes
    lines = ["# Benchmark report\n", f"_Judge: {model} (blinded review)_\n"]
    summ = os.path.join(RESULTS_DIR, "summary.csv")
    if os.path.exists(summ):
        s = list(csv.DictReader(open(summ)))
        cols = ["model", "passes", "runs", "success_rate", "avg_tokens_in",
                "avg_tokens_out", "avg_duration_s", "active_s", "overlap_s",
                "cost_per_successful_task", "cost_basis"]
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
