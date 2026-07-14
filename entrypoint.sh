#!/bin/sh
# Runs as root, then drops to `bench`. The mounted /var/run/docker.sock has a host-dependent
# owning group — root (gid 0) on Docker Desktop, `docker` (e.g. 999) on a Linux daemon. Add bench
# to whatever gid the socket actually shows inside the container, so `docker ps` works as bench
# with nothing more than `-v /var/run/docker.sock:/var/run/docker.sock` on the run command.
set -e

SOCK=/var/run/docker.sock
if [ -S "$SOCK" ]; then
  SOCK_GID=$(stat -c '%g' "$SOCK")
  if [ "$SOCK_GID" = "0" ]; then
    usermod -aG root bench
  else
    # getent exits non-zero when no group owns this gid; that's expected — the empty-GRP
    # branch then creates one. `|| true` documents that we ignore the miss under `set -e`.
    GRP=$(getent group "$SOCK_GID" | cut -d: -f1 || true)
    if [ -z "$GRP" ]; then GRP=dockerhost; groupadd -g "$SOCK_GID" "$GRP"; fi
    usermod -aG "$GRP" bench
  fi
fi

exec gosu bench "$@"
