#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Cost/quality benchmark for coding agents: same tasks, many models, one harness
# (opencode). Writes results/manifest.csv + per-run logs, then aggregate.py ->
# results/summary.csv (cost per successful task).
#
# A task is a dir under tasks/<name>/:
#   prompt.txt   (required) instruction given to the agent
#   verify.sh    (optional) exit 0 = success; runs in the work dir
#   setup.sh     (optional) runs in the work dir BEFORE the agent; gets $TASK_REPO_SRC
#   one source:  repo/ (self-contained) | repo.path (local git) | repo.git (<url> [ref])
# ---------------------------------------------------------------------------
set -euo pipefail

# Each entry is  <harness>:<model-ref>  (harness = opencode | claude). The same model can
# appear under both harnesses on purpose (model-isolation vs real-world Claude Code comp).
# All modal* GLM arms (max / high / off) run concurrently on the one endpoint; Opus/Claude-Code after.
MODELS_STR="opencode:modal/zai-org/GLM-5.2-FP8,opencode:modal-high/zai-org/GLM-5.2-FP8,opencode:modal-nothink/zai-org/GLM-5.2-FP8,opencode:anthropic/claude-opus-4-8,claude:anthropic/claude-opus-4-8"
TASKS_DIR="./tasks"
PROMPT_FILE="prompt.txt"   # per-task prompt to use; e.g. --prompt prompt.v1.txt for the baseline arm
RUNS=3
TIMEOUT_SECS=500
RETRIES=2
RUN_DELAY=2
JOBS=30
KEEP_REPO=1
CCUSAGE="npx -y ccusage"   # -y: auto-install without the interactive prompt (would hang the run)
MODEL_ENTRIES=()           # collected from repeatable --model

usage() {
  cat >&2 <<EOF
Usage: ./run_bench.sh [options]
  -r, --runs N          repeats per (harness,model,task)  [$RUNS]
  -m, --models "a,b"    comma/space list of harness:model [$MODELS_STR]
      --model H:REF     add one harness:model (repeatable), e.g. --model claude:anthropic/claude-opus-4-8
  -t, --tasks DIR       tasks directory                   [$TASKS_DIR]
      --prompt FILE     per-task prompt filename          [$PROMPT_FILE]  (e.g. prompt.v1.txt = baseline arm)
  -j, --jobs N          max task×run jobs in parallel WITHIN a group; groups (harness,model) run one at a time [$JOBS]
      --timeout SECS    kill a stuck agent                [$TIMEOUT_SECS]
      --retries N       retries on opencode server err    [$RETRIES]
      --delay SECS      pause between a group's runs       [$RUN_DELAY]
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
    --prompt) PROMPT_FILE="$2"; shift 2;;
    -j|--jobs) JOBS="$2"; shift 2;;
    --timeout) TIMEOUT_SECS="$2"; shift 2;;
    --retries) RETRIES="$2"; shift 2;;
    --delay) RUN_DELAY="$2"; shift 2;;
    --delete-repo) KEEP_REPO=0; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown arg: $1" >&2; usage; exit 1;;
  esac
done

# explicit --model wins; otherwise split the (default or -m) MODELS_STR
if [ "${#MODEL_ENTRIES[@]}" -eq 0 ]; then
  read -r -a MODEL_ENTRIES <<< "${MODELS_STR//,/ }"
fi

# split each "harness:ref" into parallel arrays HARN[] / MREF[] (bare ref defaults to opencode)
HARN=() MREF=()
for e in "${MODEL_ENTRIES[@]}"; do
  case "$e" in
    opencode:*) HARN+=("opencode"); MREF+=("${e#opencode:}");;
    claude:*)   HARN+=("claude");   MREF+=("${e#claude:}");;
    *)          HARN+=("opencode"); MREF+=("$e");;
  esac
done

model_id() { echo "${1#*/}"; }   # anthropic/claude-opus-4-8 -> claude-opus-4-8 (for `claude --model`)

if [ -f .env ]; then
  source .env
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${RESULTS_DIR:-$SCRIPT_DIR/results}"   # override for smoke tests without clobbering ./results
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

# single-run guard: refuse to start if another run_bench is already using this RESULTS_DIR
# (two concurrent runs share manifest.csv + run dirs and clobber each other).
LOCK="$RESULTS_DIR/.run.lock"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "another run_bench is already using $RESULTS_DIR (pid $(cat "$LOCK"))." >&2
  echo "  -> wait for it, kill it, or run with RESULTS_DIR=/some/other/dir ./run_bench.sh" >&2
  exit 1
fi
echo $$ > "$LOCK"
_proxies=()
_cleanup() { for p in "${_proxies[@]:-}"; do kill "$p" 2>/dev/null; done; rm -f "$LOCK"; }
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
TO="$(command -v timeout || command -v gtimeout || true)"   # macOS: brew install coreutils
now() { python3 -c 'import time; print(time.time())'; }
MANIFEST="$RESULTS_DIR/manifest.csv"
echo "task,harness,model,run,outdir,start,end,duration_s,status" > "$MANIFEST"

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

run_one_job() {   # task_name task_abs prompt harness model run
  set +e   # one bad task must not abort the whole batch
  local task_name="$1" task_abs="$2" prompt="$3" harness="$4" model="$5" run="$6"
  local safe outdir work state_dir src agent_path start end dur status attempt
  safe="$(echo "${harness}_${model}" | tr '/ :' '___')"
  outdir="$RESULTS_DIR/${task_name}__${safe}__run${run}"; mkdir -p "$outdir"
  _log ">>> $task_name | $harness | $model | run $run"

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
        ( cd "$work" && PATH="$agent_path" ${TO:+$TO ${TIMEOUT_SECS}s} \
            claude -p "$prompt" --model "$(model_id "$model")" --output-format stream-json \
            --verbose --dangerously-skip-permissions > "$outdir/output.log" 2>&1 ) || true ;;
      *)        # opencode
        ( cd "$work" && PATH="$agent_path" ${TO:+$TO ${TIMEOUT_SECS}s} \
            opencode run "$prompt" -m "$model" --format json --auto > "$outdir/output.log" 2>&1 ) || true ;;
    esac
    end="$(now)"
    perl -i -pe 's/\e\[[0-9;]*[A-Za-z]//g' "$outdir/output.log" 2>/dev/null || true
    if grep -qi "UnknownError\|Unexpected server error" "$outdir/output.log" && [ "$attempt" -lt $((RETRIES + 1)) ]; then
      _log "    server error — retry $attempt after ${RUN_DELAY}s"; attempt=$((attempt + 1)); sleep "$RUN_DELAY"; continue
    fi
    break
  done
  dur="$(python3 -c "print(f'{$end - $start:.2f}')")"

  status="n/a"
  if [ -f "$task_abs/verify.sh" ]; then
    ( cd "$work" && PATH="$agent_path" bash "$task_abs/verify.sh" ) > "$outdir/verify.log" 2>&1 \
      && status="pass" || status="fail"
  fi

  case "$harness" in
    claude) : ;;   # cost/usage/efficiency come from output.log (stream-json), parsed by aggregate.py
    *) _snapshot_usage "$state_dir" "$outdir/usage.json" ;;
  esac
  _manifest_append "$task_name,$harness,$model,$run,$outdir,$start,$end,$dur,$status"
  _log "    done ($status, ${dur}s)"
  [ "$KEEP_REPO" = "1" ] && { mkdir -p "$outdir/final_repo"; cp -R "$work/." "$outdir/final_repo/"; }
  rm -rf "$work" "$state_dir"
}

# Groups run ONE AT A TIME (sequential across (harness,model)). WITHIN a group, every task × run
# runs in PARALLEL (up to JOBS). So a model's runs all go together — e.g. all GLM runs hit the
# endpoint concurrently (the packed scenario we want to measure) — and one model's contention is
# never mixed with another model's runs, keeping each model's numbers clean.
run_group() {
  local harness="$1" model="$2" pids=() task tn ta pr run
  for task in "$TASKS_DIR"/*/; do
    [ -f "$task/prompt.txt" ] || continue
    local pf="$task/$PROMPT_FILE"; [ -f "$pf" ] || pf="$task/prompt.txt"   # fall back if arm file missing
    tn="$(basename "$task")"; ta="$(cd "$task" && pwd)"; pr="$(cat "$pf")"
    for run in $(seq 1 "$RUNS"); do
      while [ "$(jobs -pr | wc -l)" -ge "$JOBS" ]; do sleep 0.3; done
      run_one_job "$tn" "$ta" "$pr" "$harness" "$model" "$run" &
      pids+=($!)
    done
  done
  [ "${#pids[@]}" -gt 0 ] && wait "${pids[@]}" 2>/dev/null || true   # finish this group before the next
}

# Warm the GLM endpoint up front: a cold / scaled-to-zero 8xB200 returns 503 for a while, and
# without this every GLM task would hang on 503 until its own timeout (500s x N tasks wasted).
# POST a 1-token request and poll until 200; if it never comes up, fail fast (bring it up with
# ./setup.sh) instead of running the whole GLM group against a dead endpoint.
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
  if [ "${HARN[$i]}" = "opencode" ]; then case "${MREF[$i]}" in
    modal*/*)
      warm_modal "$(model_id "${MREF[$i]}")" || {
        echo "GLM endpoint $MODAL_ENDPOINT not ready — bring it up (./setup.sh), then re-run." >&2
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
RESULTS_DIR="$RESULTS_DIR" python3 "$SCRIPT_DIR/aggregate.py"