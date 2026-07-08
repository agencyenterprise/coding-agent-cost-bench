"""
Hand-rolled Modal *App* serving GLM-5.2-FP8 with SGLang — the counterpart to Modal's managed
*Auto-Endpoint* (AEP). bench.sh points MODAL_ENDPOINT at whichever, so the two hosting modes are
benchmarked through the identical harness; the only variable is AEP (managed) vs App (this).

This mirrors the AEP's actual serving config (SGLang image + server args + env, from the deployed
auto-endpoint source) so it's a *fair* replica — EXCEPT the GPU, which the AEP hardcodes to 8×B200
and we expose as a default you can override (the whole point of the App path: try cheaper tiers).

    modal deploy modal_app.py                                  # 8×B200 (default), like the AEP
    APP_GPU_TYPE=B200 APP_N_GPUS=4 modal deploy modal_app.py    # a cheaper tier to compare
    # -> OpenAI API at  https://<workspace>--glm-5-2-app-<...>.modal.run/v1
    # setup_app.sh does this and prints the /v1 URL; run_app.sh captures it.

Auth: requires_proxy_auth=True, so the same Modal-Key/Modal-Secret headers bench.sh sends work.

⚠️  Deploy + smoke-test before trusting numbers: `--tp-size` follows APP_N_GPUS, but some GPU counts
need arg changes (attention/MoE sharding), and the weights volume must actually hold the model
(set APP_MODEL_PATH / APP_WEIGHTS_VOLUME). The server args below are copied from the live AEP.
"""
import os
import subprocess

import modal

# --- GPU: default to the AEP's 8×B200, overridable at deploy time (don't hardcode) ---
GPU_TYPE = os.environ.get("APP_GPU_TYPE", "B200")
N_GPUS = int(os.environ.get("APP_N_GPUS", "8"))
GPU = f"{GPU_TYPE}:{N_GPUS}"

SERVED_MODEL_NAME = "zai-org/GLM-5.2-FP8"
MODEL_REVISION = "a0b55e88465d1a06afece97bc8d6b366aff39089"   # pinned, matches the AEP
# Where the weights live. Default: the repo id, which SGLang downloads into the mounted HF-cache
# volume on first cold start (persists after). Point APP_MODEL_PATH at a pre-populated volume path
# (e.g. /weights/zai-org/GLM-5.2-FP8) to skip the ~700 GB download.
WEIGHTS_VOLUME = os.environ.get("APP_WEIGHTS_VOLUME", "glm-app-hf-cache")
WEIGHTS_MOUNT = "/weights"
MODEL_PATH = os.environ.get("APP_MODEL_PATH", SERVED_MODEL_NAME)

PORT = 8000
MINUTES = 60
SCALEDOWN_WINDOW = 5 * MINUTES        # match the AEP
TARGET_INPUTS = 16                    # match the AEP target_concurrency
STARTUP_TIMEOUT = 60 * MINUTES

# SGLang image + env, copied from the live AEP (GLM-5.2-specific perf knobs).
SGLANG_IMAGE_TAG = "lmsysorg/sglang:v0.5.13.post1-cu130"
IMAGE_ENV = {
    "HF_XET_HIGH_PERFORMANCE": "1",
    "SGLANG_DSA_ENABLE_MTP_PRECOMPUTE_METADATA": "1",
    "SGLANG_DSA_FUSE_TOPK": "1",
    "SGLANG_ENABLE_SPEC_V2": "1",
    "SGLANG_NSA_FORCE_MLA": "1",
    "SGLANG_TIMEOUT_KEEP_ALIVE": "300",
    "TORCHINDUCTOR_COMPILE_THREADS": "1",
    "HF_HOME": WEIGHTS_MOUNT,          # cache/download weights into the mounted volume
}

# SGLang server args, copied from the live AEP (the correct GLM-5.2-FP8 flags).
SERVER_ARGS = [
    "--served-model-name", SERVED_MODEL_NAME,
    "--chunked-prefill-size", "32768",
    "--cuda-graph-max-bs", "32",
    "--disable-cuda-graph-padding",
    "--disable-piecewise-cuda-graph",
    "--dist-timeout", "3600",
    "--dsa-decode-backend", "trtllm",
    "--dsa-prefill-backend", "trtllm",
    "--dsa-topk-backend", "sgl-kernel",
    "--fp8-gemm-backend", "deep_gemm",
    "--kv-cache-dtype", "fp8_e4m3",
    "--mem-fraction-static", "0.85",
    "--reasoning-parser", "glm45",
    "--speculative-algorithm", "EAGLE",
    "--speculative-eagle-topk", "1",
    "--speculative-num-draft-tokens", "7",
    "--speculative-num-steps", "6",
    "--tool-call-parser", "glm47",
    "--trust-remote-code",
]

image = (
    modal.Image.from_registry(SGLANG_IMAGE_TAG)
    # The base image + Modal's injected client can leave typing_extensions too old for the bundled
    # pydantic_core -> "ImportError: cannot import name 'Sentinel' from typing_extensions" at sglang
    # import. Sentinel landed in typing_extensions 4.13, so pin it forward. (The AEP sidesteps this
    # via its `.uv_pip_install("autoinference-utils==0.2.2")`, which pulls a modern typing_extensions.)
    .pip_install("typing_extensions>=4.13.0")
    .env(IMAGE_ENV)
)
app = modal.App("glm-5-2-app-benchmark")
weights = modal.Volume.from_name(WEIGHTS_VOLUME, create_if_missing=True)

# HF auth for the weight download: unauthenticated pulls of a ~700 GB model get rate-limited (and
# gated repos 401). Pass HF_TOKEN from your .env (setup_app.sh sources it before `modal deploy`) into
# the container as a secret. No token -> unauthenticated (slow) download, same as before.
_HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
SECRETS = [modal.Secret.from_dict({"HF_TOKEN": _HF_TOKEN})] if _HF_TOKEN else []


@app.function(
    image=image,
    gpu=GPU,
    cpu=4,
    memory=16384,
    volumes={WEIGHTS_MOUNT: weights},
    secrets=SECRETS,
    min_containers=1,
    scaledown_window=SCALEDOWN_WINDOW,
    timeout=STARTUP_TIMEOUT,
)
@modal.concurrent(max_inputs=TARGET_INPUTS)
@modal.web_server(port=PORT, startup_timeout=STARTUP_TIMEOUT, requires_proxy_auth=True)
def serve() -> None:
    cmd = [
        "python", "-m", "sglang.launch_server",
        "--model-path", MODEL_PATH,
        "--revision", MODEL_REVISION,
        "--host", "0.0.0.0",
        "--port", str(PORT),
        "--tp-size", str(N_GPUS),
        *SERVER_ARGS,
    ]
    subprocess.Popen(cmd)
