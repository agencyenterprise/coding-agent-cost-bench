#!/usr/bin/env bash
# Runs in the fresh work dir BEFORE the agent. Pre-scaffolds the Next.js app
# and installs all mandatory deps so the agent spends its budget on logic, not
# package installation. The harness snapshots this state as the baseline commit.
set -e

node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$node_major" -lt 20 ]; then
  for v in "$HOME"/.nvm/versions/node/v2[0-9]* "$HOME"/.nvm/versions/node/v[3-9][0-9]*; do
    [ -x "$v/bin/node" ] && { export PATH="$v/bin:$PATH"; break; }
  done
fi

# Git baseline first (create-next-app checks for an existing repo via --no-git)
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git init -q
  git -c user.email=bench@local -c user.name=bench commit -q --allow-empty -m "bench: empty baseline"
fi

# Scaffold into a lowercase-named subdir to avoid npm name restrictions
# (the work dir name is a random temp path that may contain capital letters).
npx --yes create-next-app@latest kanban-app \
  --typescript \
  --tailwind \
  --app \
  --eslint \
  --no-git \
  --no-src-dir \
  --import-alias "@/*"

# Move everything (including dotfiles) up to the workspace root
cp -r kanban-app/. .
rm -rf kanban-app

# Install mandatory runtime deps (stack constraints from the task)
npm install \
  @dnd-kit/core \
  @dnd-kit/sortable \
  @dnd-kit/utilities \
  @tanstack/react-query

# Install test / dev deps so the agent can configure and run tests immediately
npm install --save-dev \
  vitest \
  @vitejs/plugin-react \
  jsdom \
  @testing-library/react \
  @testing-library/dom \
  @testing-library/user-event \
  @testing-library/jest-dom \
  @vitest/coverage-v8
