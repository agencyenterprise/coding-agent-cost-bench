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
  -j, --jobs N          total parallel worker slots, kept full; jobs drain in harness/model order (modal first) [$JOBS]
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

# Per-worker chatter goes to bench.log so it never corrupts the in-place live monitor (which the
# main pool loop owns). Detailed per-run numbers (calls_s, tokens, cost) come from report.html.
BENCHLOG="$RESULTS_DIR/bench.log"; : > "$BENCHLOG"
_jlog() { echo "$*" >> "$BENCHLOG"; }

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
  _jlog ">>> $task_name | $pv | $harness | $model | run $run"

  work="$(mktemp -d)"; state_dir="$(mktemp -d)"
  export XDG_DATA_HOME="$state_dir" XDG_STATE_HOME="$state_dir" XDG_CONFIG_HOME="$state_dir"
  src="$(prepare_work "$task_abs" "$work")"

  if [ -f "$task_abs/setup.sh" ]; then
    ( cd "$work" && TASK_REPO_SRC="$src" bash "$task_abs/setup.sh" ) > "$outdir/setup.log" 2>&1 \
      || _jlog "    setup.sh failed (see $outdir/setup.log)"
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
      _jlog "    server error — retry $attempt after ${RUN_DELAY}s"; attempt=$((attempt + 1)); sleep "$RUN_DELAY"; continue
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
  _jlog "    done ($status, ${dur}s)"
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

# ONE global queue of every (harness,model,task,prompt,run) job, ordered by harness/model exactly
# as given (so all modal* GLM setups come first, Claude last), then by task. A single pool of JOBS
# slots is kept FULL: the instant any job finishes, the next queued one launches, even across
# setups — no per-group barrier, so the endpoint stays saturated end to end. Dollars stay honest
# because aggregate.py attributes the real bill by concurrency (each second split among whoever was
# actually generating), and Claude runs on a different provider so it never muddies GLM's call_s.
QUEUE=()
build_queue() {
  local i h m task tn ta run pf pfiles p
  for i in "${!MREF[@]}"; do
    h="${HARN[$i]}"; m="${MREF[$i]}"
    for task in "$TASKS_DIR"/*/; do
      [ -f "$task/prompt.v1.txt" ] || continue
      tn="$(basename "$task")"
      [ -n "$ONLY_TASK" ] && [ "$tn" != "$ONLY_TASK" ] && continue   # --task: run only this one
      ta="$(cd "$task" && pwd)"
      pfiles=()   # --prompts restricts; else every prompt.v*.txt present
      if [ "${#PROMPT_FILES[@]}" -gt 0 ]; then
        for pf in "${PROMPT_FILES[@]}"; do [ -f "$ta/$pf" ] && pfiles+=("$pf"); done
      else
        for p in "$ta"/prompt.v*.txt; do [ -f "$p" ] && pfiles+=("$(basename "$p")"); done
      fi
      for pf in "${pfiles[@]}"; do
        for run in $(seq 1 "$RUNS"); do
          QUEUE+=("$h"$'\t'"$m"$'\t'"$tn"$'\t'"$ta"$'\t'"$pf"$'\t'"$run")
        done
      done
    done
  done
}

# short setup label for the monitor, e.g. GLM(high), Opus
setup_label() {   # harness model
  case "$2" in
    modal-high/*)    echo "GLM(high)";;
    modal-nothink/*) echo "GLM(no-think)";;
    modal/*)         echo "GLM(default)";;
    *) [ "$1" = claude ] && echo "Opus" || echo "${2##*/}";;
  esac
}

# Live "not stuck?" monitor (TTY only). Redraws in place a boxed table of the IN-FLIGHT jobs. Both
# harnesses stream NDJSON events to output.log as they work (opencode: step_start/tool_use/
# step_finish/text; Claude: stream-json), so a job's real progress signal is its log GROWING. Columns:
# setup, short task id, pid, elapsed, log size, and "quiet" = seconds since output.log last changed.
# A job goes ⚠ once it's been quiet past STALL_SECS (likely stuck), separate from nearing the timeout.
# The main loop owns the terminal; workers only write to bench.log, so nothing fights the redraw.
LIVE=0; [ -t 2 ] && LIVE=1
MON_LINES=0
STALL_SECS="${STALL_SECS:-120}"    # no output.log change in this long => flag as maybe stuck
SW=13 TW=15 PW=7 EW=7 OW=6 IW=6    # column inner widths: setup, task, pid, elapsed, log, quiet
_seg() { local n="$1" s=""; while [ "$n" -gt 0 ]; do s+="─"; n=$((n - 1)); done; printf '%s' "$s"; }
_mrow() { printf '%s─┬─%s─┬─%s─┬─%s─┬─%s─┬─%s' "$(_seg $SW)" "$(_seg $TW)" "$(_seg $PW)" "$(_seg $EW)" "$(_seg $OW)" "$(_seg $IW)"; }
MON_TOP="┌─$(_mrow)─┐"
MON_MID="├─$(_mrow | tr '┬' '┼')─┤"
MON_BOT="└─$(_mrow | tr '┬' '┴')─┘"
_fmt_el() { [ "$1" -ge 60 ] && printf '%dm%02ds' $(($1 / 60)) $(($1 % 60)) || printf '%ds' "$1"; }
_fmt_sz() { [ "$1" -ge 1048576 ] && printf '%dM' $(($1 / 1048576)) || { [ "$1" -ge 1024 ] && printf '%dK' $(($1 / 1024)) || printf '%dB' "$1"; }; }

run_pool() {   # drain QUEUE through JOBS always-full slots, ordered modal-first
  local pids=() starts=() setls=() tsks=() outs=() qi=0 total="${#QUEUE[@]}" run_start now el i t
  local np ns nl nt no h m tn ta pf run done_n log sz quiet mt warn pvv safe sig last_sig="" last_draw=0
  run_start="$(date +%s)"
  while [ "$qi" -lt "$total" ] || [ "${#pids[@]}" -gt 0 ]; do
    np=(); ns=(); nl=(); nt=(); no=()         # reap finished workers, keeping the 5 arrays aligned
    for i in "${!pids[@]}"; do
      if kill -0 "${pids[$i]}" 2>/dev/null; then
        np+=("${pids[$i]}"); ns+=("${starts[$i]}"); nl+=("${setls[$i]}"); nt+=("${tsks[$i]}"); no+=("${outs[$i]}")
      fi
    done
    pids=("${np[@]:-}"); starts=("${ns[@]:-}"); setls=("${nl[@]:-}"); tsks=("${nt[@]:-}"); outs=("${no[@]:-}")
    [ -z "${pids[0]:-}" ] && { pids=(); starts=(); setls=(); tsks=(); outs=(); }
    while [ "${#pids[@]}" -lt "$JOBS" ] && [ "$qi" -lt "$total" ]; do   # fill every free slot
      IFS=$'\t' read -r h m tn ta pf run <<< "${QUEUE[$qi]}"; qi=$((qi + 1))
      run_one_job "$tn" "$ta" "$pf" "$h" "$m" "$run" &
      t="${tn#demo-swebench-}"; t="${t##*__}"     # astropy__astropy-13579 -> astropy-13579
      pvv="$(plabel "$pf")"; safe="$(echo "${h}_${m}" | tr '/ :' '___')"   # mirror run_one_job's outdir
      pids+=($!); starts+=("$(date +%s)"); setls+=("$(setup_label "$h" "$m")"); tsks+=("$t")
      outs+=("$RESULTS_DIR/${tn}__${pvv}__${safe}__run${run}")
    done
    # Redraw only when something actually changed (a job started/finished) or every 5s to refresh the
    # clock — so the cold-start stretch where nothing moves doesn't spew a line per second.
    now="$(date +%s)"; done_n=$((qi - ${#pids[@]})); sig="$done_n:${#pids[@]}"
    if [ "$LIVE" = 1 ] && { [ "$sig" != "$last_sig" ] || [ $((now - last_draw)) -ge 5 ]; }; then
      last_sig="$sig"; last_draw="$now"
      [ "$MON_LINES" -gt 0 ] && printf '\033[%dA\033[J' "$MON_LINES" >&2
      printf 'bench  done %d/%d · running %d/%d · elapsed %s\n' \
        "$done_n" "$total" "${#pids[@]}" "$JOBS" "$(_fmt_el $((now - run_start)))" >&2
      if [ "${#pids[@]}" -gt 0 ]; then
        printf '%s\n' "$MON_TOP" >&2
        printf "│ %-${SW}s │ %-${TW}s │ %-${PW}s │ %${EW}s │ %${OW}s │ %${IW}s │\n" \
          "setup" "task" "pid" "elapsed" "log" "quiet" >&2
        printf '%s\n' "$MON_MID" >&2
        for i in "${!pids[@]}"; do
          el=$((now - starts[i]))
          log="${outs[$i]}/output.log"; sz=0; quiet=$el   # no log yet => quiet since launch
          if [ -f "$log" ]; then
            sz="$(stat -f %z "$log" 2>/dev/null || echo 0)"
            mt="$(stat -f %m "$log" 2>/dev/null || echo "$now")"; quiet=$((now - mt))
          fi
          warn=""; { [ "$quiet" -gt "$STALL_SECS" ] || [ "$el" -gt $((TIMEOUT_SECS * 4 / 5)) ]; } && warn=" ⚠"
          printf "│ %-${SW}s │ %-${TW}s │ %-${PW}s │ %${EW}s │ %${OW}s │ %${IW}s │%s\n" \
            "${setls[$i]}" "${tsks[$i]}" "${pids[$i]}" "$(_fmt_el "$el")" "$(_fmt_sz "$sz")" "$(_fmt_el "$quiet")" "$warn" >&2
        done
        printf '%s\n' "$MON_BOT" >&2
        MON_LINES=$((4 + ${#pids[@]}))
      else
        MON_LINES=1
      fi
    fi
    sleep 1
  done
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
build_queue
echo ">>> ${#QUEUE[@]} jobs across ${#MREF[@]} setups — global pool, $JOBS slots kept full, ordered modal-first" >&2
run_pool
trap - INT TERM
[ "$LIVE" = 1 ] && [ "$MON_LINES" -gt 0 ] && printf '\033[%dA\033[J' "$MON_LINES" >&2   # clear the monitor

echo "Done -> $MANIFEST"
python3 "$SCRIPT_DIR/aggregate.py" --results-dir "$RESULTS_DIR" --rate "$GLM_RATE"