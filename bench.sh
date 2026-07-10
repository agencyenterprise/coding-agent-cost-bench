#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Cost/quality benchmark for coding agents: same tasks, many models, one harness
# (opencode). Writes results/manifest.csv + per-run logs, then aggregate.py ->
# results/summary.csv (cost per successful task).
#
# A task is a dir under tasks/<name>/ (a SWE-bench Verified instance — see SWEBENCH.md):
#   prompt.v1.txt (required) baseline instruction; prompt.v2/v3.txt (optional) variants
#   setup.sh     (optional) runs in the work dir BEFORE the agent; gets $TASK_REPO_SRC
#   test.patch   marks a SWE task; grading is Modal-exclusive (grade_swe.sh -> resolved.json)
#   repo.git     the source: <url> [ref], cloned fresh per run
# Grading is on Modal only — there is no host verify.sh.
# ---------------------------------------------------------------------------
set -euo pipefail

# Each entry is  <harness>:<model-ref>  (harness = opencode | claude). The same model can appear
# under different harnesses on purpose (model-isolation vs real-world agent-loop comparison).
#   - opencode:   opencode drives the model directly (OpenAI-compatible for GLM via the modal* providers)
#   - claude:     Claude Code's own CLI + auth (Anthropic models only)
# All modal* GLM arms (default / high / off) run concurrently on the one endpoint; Opus/Claude-Code after.
MODELS_STR="opencode:modal/zai-org/GLM-5.2-FP8,opencode:modal-high/zai-org/GLM-5.2-FP8,opencode:modal-nothink/zai-org/GLM-5.2-FP8,claude:anthropic/claude-opus-4-8"
TASKS_DIR="./tasks"
ONLY_TASK=""               # run just one task (its dir name under $TASKS_DIR); empty = all
PROMPTS_STR="prompt.v2.txt"  # per-task prompt files to run (comma/space); default = v2 (shaped) only.
                             #   --prompts "prompt.v1.txt prompt.v2.txt prompt.v3.txt"  runs the full sweep
RUNS=3
TIMEOUT_SECS=600
RETRIES=2
RUN_DELAY=2
JOBS=30
KEEP_REPO=1
CCUSAGE="npx -y ccusage"   # -y: auto-install without the interactive prompt (would hang the run)
MODEL_ENTRIES=()           # collected from repeatable --model

usage() {
  cat >&2 <<EOF
Usage: ./bench.sh [options]
  -r, --runs N          repeats per (harness,model,task)  [$RUNS]
  -m, --models "a,b"    comma/space list of harness:model [$MODELS_STR]
      --model H:REF     add one harness:model (repeatable), e.g. --model claude:anthropic/claude-opus-4-8
  -t, --tasks DIR       tasks directory                   [$TASKS_DIR]
      --task NAME       run ONLY this task (dir name under --tasks), e.g. --task demo-swebench-psf__requests-6028
      --prompts LIST    per-task prompt files to run (comma/space)     [$PROMPTS_STR]
      --prompt FILE     alias for --prompts with one file; pass "prompt.v1.txt prompt.v2.txt prompt.v3.txt" for the full sweep
  -j, --jobs N          max task×run jobs in parallel WITHIN a group; groups (harness,model) run one at a time [$JOBS]
      --timeout SECS    kill a stuck agent                [$TIMEOUT_SECS]
      --retries N       retries on opencode server err    [$RETRIES]
      --delay SECS      pause between a group's runs       [$RUN_DELAY]
      --results-dir DIR where results land                [./results]
      --rate USD_PER_HR GLM GPU \$/hr for the cost calc    [50.7]
      --delete-repo     discard the mutated repo          [$KEEP_REPO]
  -h, --help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -r|--runs) RUNS="$2"; shift 2;;
    -m|--models) MODELS_STR="$2"; shift 2;;
    --model) MODEL_ENTRIES+=("$2"); shift 2;;
    -t|--tasks) TASKS_DIR="$2"; shift 2;;
    --task) ONLY_TASK="$2"; shift 2;;   # run only this task (dir name under --tasks)
    --prompt|--prompts) PROMPTS_STR="$2"; shift 2;;   # restrict to these files; else all prompt*.txt
    -j|--jobs) JOBS="$2"; shift 2;;
    --timeout) TIMEOUT_SECS="$2"; shift 2;;
    --retries) RETRIES="$2"; shift 2;;
    --delay) RUN_DELAY="$2"; shift 2;;
    --results-dir) RESULTS_DIR_ARG="$2"; shift 2;;   # where results land (was the RESULTS_DIR env)
    --rate) GLM_RATE="$2"; shift 2;;                  # GLM GPU $/hr for aggregate (was GLM_GPU_HOURLY_USD env)
    --delete-repo) KEEP_REPO=0; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown arg: $1" >&2; usage; exit 1;;
  esac
done

# --task: fail fast if the named task doesn't exist (else it'd silently run nothing)
if [ -n "$ONLY_TASK" ] && [ ! -f "$TASKS_DIR/$ONLY_TASK/prompt.v1.txt" ]; then
  echo "no task '$ONLY_TASK' in $TASKS_DIR (need $TASKS_DIR/$ONLY_TASK/prompt.v1.txt)" >&2
  echo "available: $(cd "$TASKS_DIR" 2>/dev/null && ls -d */ 2>/dev/null | tr -d / | tr '\n' ' ')" >&2
  exit 1
fi

# explicit --model wins; otherwise split the (default or -m) MODELS_STR
if [ "${#MODEL_ENTRIES[@]}" -eq 0 ]; then
  read -r -a MODEL_ENTRIES <<< "${MODELS_STR//,/ }"
fi

# split each "harness:ref" into parallel arrays HARN[] / MREF[] (bare ref defaults to opencode)
HARN=() MREF=()
for e in "${MODEL_ENTRIES[@]}"; do
  case "$e" in
    opencode:*)   HARN+=("opencode");   MREF+=("${e#opencode:}");;
    claude:*)     HARN+=("claude");     MREF+=("${e#claude:}");;
    *)            HARN+=("opencode");   MREF+=("$e");;
  esac
done

model_id() { echo "${1#*/}"; }   # anthropic/claude-opus-4-8 -> claude-opus-4-8 (for `claude --model`)

# prompt version label from a filename: prompt.v1.txt -> v1 (baseline), prompt.v2.txt -> v2 (shaped
# template), prompt.<x>.txt -> x. Recorded in the manifest as the `prompt` column.
plabel() { local f="${1##*/}"; f="${f#prompt.}"; echo "${f%.txt}"; }

# restrict set (empty = discover all prompt*.txt per task in run_group)
read -r -a PROMPT_FILES <<< "${PROMPTS_STR//,/ }"

if [ -f .env ]; then
  source .env
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${RESULTS_DIR_ARG:-$SCRIPT_DIR/results}"   # --results-dir; default ./results
GLM_RATE="${GLM_RATE:-50.7}"                            # --rate; GLM GPU $/hr passed to aggregate
CACHE_DIR="$SCRIPT_DIR/.cache/repos"   # remote repos cloned once, reused per run
export OPENCODE_CONFIG="$SCRIPT_DIR/opencode.jsonc"   # honored from /tmp clones too
export NO_COLOR=1                                     # no ANSI in logs
unset ANTHROPIC_BASE_URL OPENAI_BASE_URL GOOGLE_BASE_URL 2>/dev/null || true  # avoid 404 hijack

# preflight: fail fast if a selected harness/provider requirement is missing
_miss=""
for i in "${!MREF[@]}"; do
  if [ "${HARN[$i]}" = "claude" ]; then
    command -v claude >/dev/null 2>&1 || _miss="$_miss claude-CLI"   # Claude Code uses its own auth
    continue
  fi
  case "${MREF[$i]}" in
    modal/*)     for v in MODAL_ENDPOINT MODAL_KEY MODAL_SECRET; do [ -z "${!v:-}" ] && _miss="$_miss $v"; done;;
    anthropic/*) [ -z "${ANTHROPIC_API_KEY:-}" ] && _miss="$_miss ANTHROPIC_API_KEY";;
    openai/*)    [ -z "${OPENAI_API_KEY:-}" ]    && _miss="$_miss OPENAI_API_KEY";;
    google/*)    [ -z "${GEMINI_API_KEY:-}" ]    && _miss="$_miss GEMINI_API_KEY";;
  esac
done
if [ -n "$_miss" ]; then
  echo "missing env vars:$(echo "$_miss" | tr ' ' '\n' | sort -u | tr '\n' ' ')" >&2
  echo "  -> fill .env and 'source .env' (see .env.example)" >&2
  exit 1
fi

mkdir -p "$RESULTS_DIR" "$CACHE_DIR"

# single-run guard: refuse to start if another bench.sh run is already using this RESULTS_DIR
# (two concurrent runs share manifest.csv + run dirs and clobber each other).
LOCK="$RESULTS_DIR/.run.lock"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "another bench.sh run is already using $RESULTS_DIR (pid $(cat "$LOCK"))." >&2
  echo "  -> wait for it, kill it, or run with RESULTS_DIR=/some/other/dir ./bench.sh" >&2
  exit 1
fi
echo $$ > "$LOCK"
_proxies=()
_cleanup() {
  for p in "${_proxies[@]:-}"; do kill "$p" 2>/dev/null; done
  rm -f "$LOCK"
}
trap _cleanup EXIT

# One reasoning proxy per proxied GLM arm (opencode can't add chat_template_kwargs itself).
# Separate proxy/port per tier so max/high/off can run concurrently against the same endpoint.
_start_proxy() {  # mode port endpoint-var
  echo "starting reasoning proxy [$1] on :$2" >&2
  python3 "$SCRIPT_DIR/reasoning_proxy.py" --reasoning "$1" --port "$2" > "$RESULTS_DIR/proxy_$1.log" 2>&1 &
  _proxies+=($!); export "$3=http://127.0.0.1:$2/v1"; sleep 1
}
pkill -f "$SCRIPT_DIR/reasoning_proxy.py" 2>/dev/null || true   # clear leftovers from a hard-killed run
_off="" _high=""
for i in "${!MREF[@]}"; do case "${MREF[$i]}" in
  modal-nothink/*) [ -z "$_off" ]  && { _start_proxy off  "${NOTHINK_PORT:-8899}" NOTHINK_ENDPOINT; _off=1; } ;;
  modal-high/*)    [ -z "$_high" ] && { _start_proxy high "${HIGH_PORT:-8898}"    HIGH_ENDPOINT;    _high=1; } ;;
esac; done

$CCUSAGE --help >/dev/null 2>&1 || true   # pre-install ccusage once so per-run usage.json is clean JSON
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"   # macOS: brew install coreutils
# Prefer an explicit helper over `${TO:+$TO ${TIMEOUT_SECS}s}` — that word-split form is fragile
# under set -u / accidental TO overwrite (can turn argv[0] into a model ref like anthropic/…).
run_with_timeout() {
  if [ -n "$TIMEOUT_BIN" ]; then "$TIMEOUT_BIN" "${TIMEOUT_SECS}s" "$@"; else "$@"; fi
}
now() { python3 -c 'import time; print(time.time())'; }
MANIFEST="$RESULTS_DIR/manifest.csv"
echo "task,harness,model,prompt,run,outdir,start,end,duration_s,status" > "$MANIFEST"

_log() { echo "$*" >&2; }

_manifest_append() {   # append one CSV line under a lock (safe with --jobs)
  local lock="$RESULTS_DIR/.manifest.lock"
  while ! mkdir "$lock" 2>/dev/null; do sleep 0.05; done
  echo "$1" >> "$MANIFEST"; rmdir "$lock"
}

prepare_work() {   # populate $2 (work dir) from the task's source; echo external src path (or "")
  local task_abs="$1" work="$2"
  if [ -d "$task_abs/repo" ]; then
    cp -R "$task_abs/repo/." "$work/"; echo ""
  elif [ -f "$task_abs/repo.path" ]; then
    local src; src="$(cat "$task_abs/repo.path")"; src="${src/#\~/$HOME}"
    git clone --local --quiet "$src" "$work"; echo "$src"
  elif [ -f "$task_abs/repo.git" ]; then
    local url ref key cache lock n
    read -r url ref < "$task_abs/repo.git"
    key="$(echo "${url}__${ref:-HEAD}" | tr -c 'A-Za-z0-9' '_')"
    cache="$CACHE_DIR/$key"; lock="$cache.lock"
    while ! mkdir "$lock" 2>/dev/null; do sleep 0.2; done      # populate cache once (safe under --jobs)
    if [ ! -d "$cache/.git" ]; then
      rm -rf "$cache"; n=1
      until git clone --quiet "$url" "$cache" 2>/dev/null; do
        [ "$n" -ge 3 ] && { rmdir "$lock"; return 1; }          # give up after 3 tries
        n=$((n + 1)); sleep 3
      done
    fi                                                          # cache stays on its default branch
    rmdir "$lock"
    git -c advice.detachedHead=false clone --local --quiet "$cache" "$work"   # fast local clone
    [ -n "${ref:-}" ] && git -C "$work" -c advice.detachedHead=false checkout --quiet "$ref"
    echo ""
  else echo ""; fi
}

# ccusage reads $HOME/.local/share/opencode/opencode.db (ignores XDG). Copy this run's
# isolated DB into a temp HOME so usage.json belongs to this job alone.
_snapshot_usage() {
  local state_dir="$1" outfile="$2" db="$1/opencode/opencode.db" home
  [ -f "$db" ] || { echo '{"sessions":[]}' > "$outfile"; return 0; }
  command -v sqlite3 >/dev/null 2>&1 && sqlite3 "$db" 'PRAGMA wal_checkpoint(FULL);' 2>/dev/null || true
  home="$(mktemp -d)"; mkdir -p "$home/.local/share/opencode"
  cp "$db" "$home/.local/share/opencode/"
  cp "$state_dir"/opencode/opencode.db-wal "$home/.local/share/opencode/" 2>/dev/null || true
  cp "$state_dir"/opencode/opencode.db-shm "$home/.local/share/opencode/" 2>/dev/null || true
  HOME="$home" $CCUSAGE opencode session --json > "$outfile" 2>/dev/null || true
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$outfile" 2>/dev/null \
    || echo '{"sessions":[]}' > "$outfile"   # guard: never leave non-JSON in usage.json
  rm -rf "$home"
}

run_one_job() {   # task_name task_abs prompt_file harness model run
  set +e   # one bad task must not abort the whole batch
  local task_name="$1" task_abs="$2" prompt_file="$3" harness="$4" model="$5" run="$6"
  local pv prompt safe outdir work state_dir src agent_path start end dur status attempt
  pv="$(plabel "$prompt_file")"                 # prompt version label (e.g. v2, v1)
  prompt="$(cat "$task_abs/$prompt_file")"
  safe="$(echo "${harness}_${model}" | tr '/ :' '___')"
  outdir="$RESULTS_DIR/${task_name}__${pv}__${safe}__run${run}"; mkdir -p "$outdir"
  _log ">>> $task_name | $pv | $harness | $model | run $run"

  work="$(mktemp -d)"; state_dir="$(mktemp -d)"
  export XDG_DATA_HOME="$state_dir" XDG_STATE_HOME="$state_dir" XDG_CONFIG_HOME="$state_dir"
  src="$(prepare_work "$task_abs" "$work")"

  if [ -f "$task_abs/setup.sh" ]; then
    ( cd "$work" && TASK_REPO_SRC="$src" bash "$task_abs/setup.sh" ) > "$outdir/setup.log" 2>&1 \
      || _log "    setup.sh failed (see $outdir/setup.log)"
  fi

  # snapshot the post-setup state so the agent's diff is isolable later (judge.py uses `git diff HEAD`)
  if git -C "$work" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$work" -c user.email=bench@local -c user.name=bench add -A >/dev/null 2>&1
    git -C "$work" -c user.email=bench@local -c user.name=bench commit -qm "post-setup baseline" >/dev/null 2>&1 || true
  fi

  agent_path="$PATH"
  [ -x "$work/.venv/bin/python" ] && agent_path="$work/.venv/bin:$agent_path"

  attempt=1
  while :; do
    start="$(now)"
    case "$harness" in
      claude)   # real-world harness: Claude Code's own CLI (Anthropic models only)
        ( cd "$work" && PATH="$agent_path" run_with_timeout \
            claude -p "$prompt" --model "$(model_id "$model")" --output-format stream-json \
            --verbose --dangerously-skip-permissions > "$outdir/output.log" 2>&1 ) || true ;;
      *)        # opencode
        ( cd "$work" && PATH="$agent_path" run_with_timeout \
            opencode run "$prompt" -m "$model" --format json --auto > "$outdir/output.log" 2>&1 ) || true ;;
    esac
    end="$(now)"
    perl -i -pe 's/\e\[[0-9;]*[A-Za-z]//g' "$outdir/output.log" 2>/dev/null || true
    # Retry transient provider/server errors (opencode/Claude Code surface these in the log).
    if grep -qi "UnknownError\|Unexpected server error" "$outdir/output.log" && [ "$attempt" -lt $((RETRIES + 1)) ]; then
      _log "    server error — retry $attempt after ${RUN_DELAY}s"; attempt=$((attempt + 1)); sleep "$RUN_DELAY"; continue
    fi
    break
  done
  dur="$(python3 -c "print(f'{$end - $start:.2f}')")"

  # Grading is Modal-exclusive: the agent's patch is graded in the instance's official SWE-bench
  # Docker image (grade_swe.sh -> swe_eval_modal.py -> resolved.json), and aggregate.py uses that for
  # pass/fail. bench.sh only GENERATES + records the patch; there is no host verify. status stays n/a
  # here and is filled from resolved.json at aggregate time (run with --swe-grade / ./grade_swe.sh).
  status="n/a"

  case "$harness" in
    claude) : ;;   # cost/usage/efficiency come from output.log (stream-json), parsed by aggregate.py
    *) _snapshot_usage "$state_dir" "$outdir/usage.json" ;;
  esac
  _manifest_append "$task_name,$harness,$model,$pv,$run,$outdir,$start,$end,$dur,$status"
  _log "    done ($status, ${dur}s)"
  # Keep the agent's change for grading/judging. For a git repo, save only the DIFF (KB): copying the
  # whole checked-out tree per run is GBs for big projects (django/astropy) and fills the disk. Only
  # non-git tasks copy the tree (there the tree IS the artifact). make_predictions.py / judge.py read
  # model.patch first, then fall back to a final_repo/ diff.
  if [ "$KEEP_REPO" = "1" ]; then
    if git -C "$work" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      git -C "$work" -c core.fileMode=false diff HEAD > "$outdir/model.patch" 2>/dev/null || true
    else
      mkdir -p "$outdir/final_repo"; cp -R "$work/." "$outdir/final_repo/"
    fi
  fi
  rm -rf "$work" "$state_dir"
}

# Groups run ONE AT A TIME (sequential across (harness,model)). WITHIN a group, every task × run
# runs in PARALLEL (up to JOBS). So a model's runs all go together — e.g. all GLM runs hit the
# endpoint concurrently (the packed scenario we want to measure) — and one model's contention is
# never mixed with another model's runs, keeping each model's numbers clean.
run_group() {
  local harness="$1" model="$2" pids=() task tn ta run pf pfiles p alive
  for task in "$TASKS_DIR"/*/; do
    [ -f "$task/prompt.v1.txt" ] || continue
    tn="$(basename "$task")"
    [ -n "$ONLY_TASK" ] && [ "$tn" != "$ONLY_TASK" ] && continue   # --task: run only this one
    ta="$(cd "$task" && pwd)"
    # which prompt versions to run for this task: --prompts restricts; else every prompt.v*.txt
    # present (prompt.v1.txt, prompt.v2.txt, ...)
    pfiles=()
    if [ "${#PROMPT_FILES[@]}" -gt 0 ]; then
      for pf in "${PROMPT_FILES[@]}"; do [ -f "$ta/$pf" ] && pfiles+=("$pf"); done
    else
      for p in "$ta"/prompt.v*.txt; do [ -f "$p" ] && pfiles+=("$(basename "$p")"); done
    fi
    for pf in "${pfiles[@]}"; do
      for run in $(seq 1 "$RUNS"); do
        # count only OUR task workers, not every background process in the shell — the reasoning
        # proxies are also backgrounded here and never exit, so `jobs -pr` alone would count against
        # $JOBS and deadlock this throttle before any worker ever launches.
        while :; do
          alive=0
          for p in "${pids[@]:-}"; do [ -n "$p" ] && kill -0 "$p" 2>/dev/null && alive=$((alive + 1)); done
          [ "$alive" -lt "$JOBS" ] && break
          sleep 0.3
        done
        run_one_job "$tn" "$ta" "$pf" "$harness" "$model" "$run" &
        pids+=($!)
      done
    done
  done
  [ "${#pids[@]}" -gt 0 ] && wait "${pids[@]}" 2>/dev/null || true   # finish this group before the next
  return 0
}

# Warm the GLM endpoint up front: a cold / scaled-to-zero 8xB200 returns 503 for a while, and
# without this every GLM task would hang on 503 until its own timeout (500s x N tasks wasted).
# POST a 1-token request and poll until 200; if it never comes up, fail fast (bring it up with
# ./setup_auto_endpoint.sh) instead of running the whole GLM group against a dead endpoint.
warm_modal() {
  local model="$1" i code tries="${WARM_TRIES:-16}"
  [ -n "${MODAL_ENDPOINT:-}" ] || { echo "MODAL_ENDPOINT unset" >&2; return 1; }
  echo "warming GLM endpoint (cold 8xB200 can take a few minutes)..." >&2
  for i in $(seq 1 "$tries"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 180 \
      -H "Modal-Key: ${MODAL_KEY:-}" -H "Modal-Secret: ${MODAL_SECRET:-}" \
      -H 'Content-Type: application/json' -X POST "$MODAL_ENDPOINT/chat/completions" \
      -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_tokens\":1}" 2>/dev/null)"
    [ "$code" = "200" ] && { echo "  endpoint ready (HTTP 200)" >&2; return 0; }
    echo "  [$i/$tries] HTTP ${code:-000} — waiting ${WARM_SLEEP:-15}s..." >&2
    sleep "${WARM_SLEEP:-15}"
  done
  return 1
}
for i in "${!MREF[@]}"; do
  # opencode modal* arms hit the Modal GLM endpoint — warm it before the run.
  if [ "${HARN[$i]}" = "opencode" ]; then case "${MREF[$i]}" in
    modal*/*)
      warm_modal "$(model_id "${MREF[$i]}")" || {
        echo "GLM endpoint $MODAL_ENDPOINT not ready — bring it up (./setup_auto_endpoint.sh), then re-run." >&2
        exit 1; }
      break ;;
  esac; fi
done

trap 'kill $(jobs -p) 2>/dev/null; wait 2>/dev/null; exit 130' INT TERM
# One (harness,model) at a time for CLEAN per-arm cost — no cross-arm contention muddying call_s.
# Within a group, tasks still run parallel (up to JOBS), so each arm is measured at its own packing.
# The modal* arms are adjacent in the matrix, so the endpoint stays warm across them (no re-cold-start).
for i in "${!MREF[@]}"; do
  echo ">>> group: ${HARN[$i]} | ${MREF[$i]} — running its tasks in parallel (up to $JOBS)" >&2
  run_group "${HARN[$i]}" "${MREF[$i]}"
done
trap - INT TERM

echo "Done -> $MANIFEST"
python3 "$SCRIPT_DIR/aggregate.py" --results-dir "$RESULTS_DIR" --rate "$GLM_RATE"