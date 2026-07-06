#!/usr/bin/env bash
# Runs in the fresh work dir BEFORE the agent. This is a from-scratch build task
# (no repo/ | repo.path | repo.git), so the work dir starts empty. Provide a
# Node >= 20 env and a git baseline so run_bench's post-setup snapshot exists
# and judge.py can isolate the agent's diff via `git diff HEAD`.
set -e

# Next.js 15/16 needs Node >= 20; fall back to an nvm Node if the host default is older.
node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$node_major" -lt 20 ]; then
  for v in "$HOME"/.nvm/versions/node/v2[0-9]* "$HOME"/.nvm/versions/node/v[3-9][0-9]*; do
    [ -x "$v/bin/node" ] && { export PATH="$v/bin:$PATH"; break; }
  done
fi

# Empty work dir -> no git repo yet. Init with an empty baseline commit so the
# harness's snapshot step (git add -A / commit) has a repo to work with.
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git init -q
  git -c user.email=bench@local -c user.name=bench commit -q --allow-empty -m "bench: empty baseline"
fi
