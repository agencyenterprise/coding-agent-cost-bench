#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Cost/quality benchmark for coding agents.
# Same tasks, many models, ONE harness (opencode) -> clean apples-to-apples.
#
# A task is a dir under tasks/ with:
#   prompt.txt   (required) the instruction given to the agent
#   verify.sh    (optional) exit 0 = success; runs in the work dir
#   setup.sh     (optional) runs in the work dir BEFORE the agent; gets $TASK_REPO_SRC
#   repo/        (optional) self-contained starting code (copied fresh)
#   repo.path    (optional) abs path to an external git repo -> cloned fresh per run
#
# Usage:
#   ./run_bench.sh [options]
#     -r, --runs N          repeats per (task,model)          [default 3]
#     -m, --models "a b"    space- or comma-separated models  [default: built-in set]
#     -t, --tasks DIR       tasks directory                   [default ./tasks]
#     -j, --jobs N          parallel (task,model,run) jobs    [default 1]
#         --timeout SECS    kill a stuck agent                [default 900]
#         --glm "modal,GLM" substrings routed to Modal GPU $  [default modal,GLM]
#         --retries N       retries on opencode server error  [default 2]
#         --delay SECS      pause between sequential runs     [default 2]
#         --keep-repo       keep the mutated repo per run
#         --no-aggregate    skip aggregate.py at the end
#     -h, --help
#
# Env vars are accepted as fallbacks (MODELS, RUNS_PER_PAIR, TIMEOUT_SECS, ...).
# ---------------------------------------------------------------------------
set -euo pipefail

# ---- defaults (env fallback, overridden by CLI args below) -----------------
MODELS_STR="${MODELS:-}"
TASKS_DIR="${TASKS_DIR:-./tasks}"
RESULTS_DIR="${RESULTS_DIR:-./results}"
RUNS_PER_PAIR="${RUNS_PER_PAIR:-3}"
TIMEOUT_SECS="${TIMEOUT_SECS:-900}"
KEEP_REPO="${KEEP_REPO:-0}"
CCUSAGE="${CCUSAGE:-npx ccusage}"
GLM_MODELS="${GLM_MODELS:-modal,GLM}"
RETRIES="${RETRIES:-2}"
RUN_DELAY="${RUN_DELAY:-2}"
JOBS="${JOBS:-1}"
AGGREGATE=1

usage() { sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    -r|--runs)       RUNS_PER_PAIR="$2"; shift 2;;
    -m|--models)     MODELS_STR="$2"; shift 2;;
    -t|--tasks)      TASKS_DIR="$2"; shift 2;;
    -j|--jobs)       JOBS="$2"; shift 2;;
    --timeout)       TIMEOUT_SECS="$2"; shift 2;;
    --glm)           GLM_MODELS="$2"; shift 2;;
    --retries)       RETRIES="$2"; shift 2;;
    --delay)         RUN_DELAY="$2"; shift 2;;
    --keep-repo)     KEEP_REPO=1; shift;;
    --no-aggregate)  AGGREGATE=0; shift;;
    -h|--help)       usage; exit 0;;
    *) echo "unknown arg: $1" >&2; usage; exit 1;;
  esac
done

if [ -n "$MODELS_STR" ]; then
  MODELS_STR="${MODELS_STR//,/ }"   # accept comma- or space-separated lists
  read -r -a MODELS <<< "$MODELS_STR"
else
  MODELS=(
    "modal/zai-org/GLM-5.2-FP8"      # GLM via your Modal endpoint (provider/model)
    "anthropic/claude-opus-4-8"      # confirm exact IDs with: opencode models
    "anthropic/claude-fable-5"
    "openai/gpt-5-codex"
    "google/gemini-2.5-pro"
  )
fi
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OPENCODE_CONFIG="${OPENCODE_CONFIG:-$SCRIPT_DIR/opencode.jsonc}"   # honored from /tmp too
export NO_COLOR="${NO_COLOR:-1}"                                          # stop ANSI codes in logs

# preflight: fail fast if a selected provider's env vars are missing
_miss=""
_need() { for v in "$@"; do [ -z "${!v:-}" ] && _miss="$_miss $v"; done; return 0; }
for m in "${MODELS[@]}"; do
  case "$m" in
    modal/*)     _need MODAL_ENDPOINT MODAL_KEY MODAL_SECRET;;
    anthropic/*) _need ANTHROPIC_API_KEY;;
    openai/*)    _need OPENAI_API_KEY;;
    google/*)    _need GEMINI_API_KEY;;
  esac
done
if [ -n "$_miss" ]; then
  echo "missing env vars:$(echo "$_miss" | tr ' ' '\n' | sort -u | tr '\n' ' ')" >&2
  echo "  -> fill .env and 'source .env' (see .env.example)" >&2
  exit 1
fi

mkdir -p "$RESULTS_DIR"
RESULTS_DIR="$(cd "$RESULTS_DIR" && pwd)"
now() { python3 -c 'import time; print(time.time())'; }         # macOS date lacks %N
TO="$(command -v timeout || command -v gtimeout || true)"       # macOS: brew install coreutils

MANIFEST="$RESULTS_DIR/manifest.csv"
echo "task,model,run,outdir,snap_index,start,end,duration_s,status" > "$MANIFEST"

prepare_work() {
  local task_abs="$1" work="$2"
  if [ -d "$task_abs/repo" ]; then
    cp -R "$task_abs/repo/." "$work/"; echo ""
  elif [ -f "$task_abs/repo.path" ]; then
    local src; src="$(cat "$task_abs/repo.path")"; src="${src/#\~/$HOME}"
    if git -C "$src" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      git clone --local --quiet "$src" "$work"
    else
      cp -R "$src/." "$work/"
    fi
    echo "$src"
  elif [ -f "$task_abs/repo.git" ]; then
    local url ref; read -r url ref < "$task_abs/repo.git"   # "<url> [ref]"  (ref = branch/tag/SHA)
    git clone --quiet "$url" "$work"
    [ -n "${ref:-}" ] && git -C "$work" -c advice.detachedHead=false checkout --quiet "$ref"
    echo ""
  else
    echo ""
  fi
}

_manifest_append() {
  local line="$1" lock="$RESULTS_DIR/.manifest.lock"
  while ! mkdir "$lock" 2>/dev/null; do sleep 0.05; done
  echo "$line" >> "$MANIFEST"
  rmdir "$lock"
}

_running_jobs() {
  jobs -pr | wc -l | tr -d ' '
}

_log() { echo "$*" >&2; }

_parallel_pids=()
_kill_parallel_jobs() {
  _log "interrupted — stopping background jobs..."
  for pid in "${_parallel_pids[@]}"; do kill "$pid" 2>/dev/null; done
  wait 2>/dev/null || true
  exit 130
}

run_one_job() {
  set +e
  local task_name="$1" task_abs="$2" prompt="$3" model="$4" run="$5"
  local safe outdir work state_dir src agent_path start end dur status
  local attempt max rc=0

  safe="$(echo "$model" | tr '/ :' '___')"
  outdir="$RESULTS_DIR/${task_name}__${safe}__run${run}"
  mkdir -p "$outdir"
  _log ">>> $task_name | $model | run $run"

  work="$(mktemp -d)"
  state_dir="$(mktemp -d)"
  export XDG_DATA_HOME="$state_dir"
  export XDG_STATE_HOME="$state_dir"
  export XDG_CONFIG_HOME="$state_dir"

  _log "    preparing workspace..."
  src="$(prepare_work "$task_abs" "$work")"

  if [ -f "$task_abs/setup.sh" ]; then
    mkdir -p "$outdir"
    ( cd "$work" && TASK_REPO_SRC="$src" bash "$task_abs/setup.sh" ) \
      > "$outdir/setup.log" 2>&1 || _log "    setup.sh FAILED (see $outdir/setup.log)"
  fi

  agent_path="$PATH"
  [ -x "$work/.venv/bin/python" ] && agent_path="$work/.venv/bin:$agent_path"

  mkdir -p "$outdir"
  $CCUSAGE opencode session --json > "$outdir/ccusage.before.json" 2>/dev/null \
    || echo '[]' > "$outdir/ccusage.before.json"

  _log "    running agent..."
  attempt=1; max=$((RETRIES + 1))
  while :; do
    start="$(now)"
    mkdir -p "$outdir"
    ( cd "$work" && PATH="$agent_path" ${TO:+$TO ${TIMEOUT_SECS}s} \
        opencode run "$prompt" -m "$model" > "$outdir/output.log" 2>&1 ) || true
    end="$(now)"
    perl -i -pe 's/\e\[[0-9;]*[A-Za-z]//g' "$outdir/output.log" 2>/dev/null || true
    if grep -qi "UnknownError\|Unexpected server error" "$outdir/output.log" && [ "$attempt" -lt "$max" ]; then
      _log "    server error — retry $attempt/$((max-1)) after ${RUN_DELAY}s"
      attempt=$((attempt + 1)); sleep "$RUN_DELAY"; continue
    fi
    break
  done
  dur="$(python3 -c "print(f'{$end - $start:.2f}')")"

  status="n/a"
  if [ -f "$task_abs/verify.sh" ]; then
    mkdir -p "$outdir"
    if ( cd "$work" && PATH="$agent_path" bash "$task_abs/verify.sh" ) > "$outdir/verify.log" 2>&1; then
      status="pass"; else status="fail"; fi
  fi

  mkdir -p "$outdir"
  $CCUSAGE opencode session --json > "$outdir/ccusage.after.json" 2>/dev/null \
    || echo '[]' > "$outdir/ccusage.after.json"

  _manifest_append "$task_name,$model,$run,$outdir,,$start,$end,$dur,$status"
  _log "    done ($status, ${dur}s)"
  [ "$KEEP_REPO" = "1" ] && { mkdir -p "$outdir/final_repo"; cp -R "$work/." "$outdir/final_repo/"; }
  rm -rf "$work" "$state_dir"
  [ "$JOBS" -le 1 ] && sleep "$RUN_DELAY"
  return "$rc"
}

JOB_TASK_NAMES=()
JOB_TASK_ABSS=()
JOB_PROMPTS=()
JOB_MODELS_LIST=()
JOB_RUNS=()
for task in "$TASKS_DIR"/*/; do
  [ -f "$task/prompt.txt" ] || continue
  task_name="$(basename "$task")"
  task_abs="$(cd "$task" && pwd)"
  prompt="$(cat "$task/prompt.txt")"
  for model in "${MODELS[@]}"; do
    for run in $(seq 1 "$RUNS_PER_PAIR"); do
      JOB_TASK_NAMES+=("$task_name")
      JOB_TASK_ABSS+=("$task_abs")
      JOB_PROMPTS+=("$prompt")
      JOB_MODELS_LIST+=("$model")
      JOB_RUNS+=("$run")
    done
  done
done

if [ "$JOBS" -le 1 ]; then
  for i in "${!JOB_TASK_NAMES[@]}"; do
    run_one_job "${JOB_TASK_NAMES[$i]}" "${JOB_TASK_ABSS[$i]}" "${JOB_PROMPTS[$i]}" \
      "${JOB_MODELS_LIST[$i]}" "${JOB_RUNS[$i]}"
  done
else
  echo "running ${#JOB_TASK_NAMES[@]} jobs with --jobs $JOBS"
  trap '_kill_parallel_jobs' INT TERM
  for i in "${!JOB_TASK_NAMES[@]}"; do
    while [ "$(_running_jobs)" -ge "$JOBS" ]; do sleep 0.3; done
    run_one_job "${JOB_TASK_NAMES[$i]}" "${JOB_TASK_ABSS[$i]}" "${JOB_PROMPTS[$i]}" \
      "${JOB_MODELS_LIST[$i]}" "${JOB_RUNS[$i]}" &
    _parallel_pids+=($!)
  done
  wait || true
  trap - INT TERM
fi

echo "Done -> $MANIFEST"
if [ "$AGGREGATE" = "1" ]; then
  echo "== aggregating =="
  GLM_MODELS="$GLM_MODELS" RESULTS_DIR="$RESULTS_DIR" python3 "$SCRIPT_DIR/aggregate.py" \
    || echo "aggregate.py failed — run it manually"
else
  echo "Next: GLM_MODELS=\"$GLM_MODELS\" python3 aggregate.py"
fi
