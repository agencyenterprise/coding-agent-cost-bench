# DeepSWE cost benchmark — one `docker run` fans pier over (setup × task × run) on contamination-free
# DeepSWE tasks and writes raw per-run results + manifest.csv to /out. The report (progress_report.html
# + CSVs) is generated locally afterward with benchmark_progress_report.py — not by this image.
#
# pier drives the HOST docker (via the mounted socket) to build & run each task's own container and
# inject the agent CLI into it — so this image ships only python + the docker CLI + pier + the
# orchestrator, NOT node/opencode/claude-code (pier installs those inside each task image).
#
#   DIR="$PWD/runs-deepswe"; mkdir -p "$DIR"
#   docker run --rm -p 80:80 \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -v "$DIR:$DIR" -e OUT_DIR="$DIR" \
#     -e HOST_IP=$(hostname -I | awk '{print $1}') \
#     --env-file .env \
#     ghcr.io/agencyenterprise/coding-agent-cost-bench \
#     --setups glm-default,glm-high,glm-nothink,opus --runs 4 --jobs 8
#   # -> $DIR/<timestamp>/ : per-run folders (output.log, reward.json, usage.json) + manifest.csv
#   #    (+ pier-jobs/). Then locally: python3 benchmark_progress_report.py $DIR/<timestamp>
#
# ONE data mount, mounted at the SAME path inside and out (-v "$DIR:$DIR"): everything for a run —
# per-run results, manifest.csv, and the pier job tree — lands under $DIR/<timestamp>/. The mount is required
# because pier drives the HOST daemon, which bind-mounts the job tree into task containers by literal
# path (docker-out-of-docker), so the path must resolve identically on both sides. Each invocation
# gets its own timestamped subfolder, so repeated runs never clobber.
#
# Creds come ONLY from the runtime env (--env-file); nothing is baked. The DeepSWE tasks ARE baked
# (the target box has no git). See entrypoint.sh.
FROM python:3.12-slim-bookworm

# docker CLI + compose/buildx plugins (client only — pier talks to the HOST daemon via the socket).
# Debian bookworm is in Docker's apt repo (unlike the EC2 host's newer Ubuntu, which is why we ship
# the CLI here rather than relying on the box).
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       docker-ce-cli docker-compose-plugin docker-buildx-plugin \
    && rm -rf /var/lib/apt/lists/*

# pier (DeepSWE runner + agent injector) — from requirements.txt so the deps have one source of
# truth. Copied first for layer caching. pier pulls its own deps (rich, typer, pyyaml, httpx,
# tenacity, …); run_deepswe.py is stdlib-only. (Reporting + Modal billing run locally, not in-image,
# via benchmark_progress_report.py.)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

WORKDIR /app
# Bake the contamination-free DeepSWE tasks (no git on the target box). Used only as docker build
# context / compose config by the CLI locally, so a container path here is fine — only the pier job
# tree (under the host-aligned OUT_DIR at runtime) must be host-aligned (see entrypoint.sh).
RUN curl -sL https://github.com/datacurve-ai/deep-swe/archive/refs/heads/main.tar.gz | tar xz
ENV TASKS_DIR=/app/deep-swe-main/tasks

COPY run_deepswe.py reasoning_proxy.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

# Runs as root: needs the docker socket and to bind :80 for the sidecar. The agent (incl. Claude
# Code, which refuses root) runs inside pier's task containers, not here, so root is fine.
# OUT_DIR is where runs land; override it at runtime to a host-aligned path (-e OUT_DIR="$DIR" with
# -v "$DIR:$DIR"). The pier job tree defaults to <OUT_DIR>/<run-id>/pier-jobs, so it's aligned too.
ENV OUT_DIR=/out
ENTRYPOINT ["/app/entrypoint.sh"]
