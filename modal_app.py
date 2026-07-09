"""
Hand-rolled Modal *App* serving GLM-5.2-FP8 with SGLang — the counterpart to Modal's managed
*Auto-Endpoint* (AEP). bench.sh points MODAL_ENDPOINT at whichever, so the two hosting modes are
benchmarked through the identical harness; the only variable is AEP (managed) vs App (this).

This mirrors the AEP's actual serving config (SGLang image + server args + env, from the deployed
auto-endpoint source) so it's a *fair* replica — EXCEPT the GPU, which the AEP hardcodes to 8×B200
and we expose as a default you can override (the whole point of the App path: try cheaper tiers).

    ./run_app.sh --gpu B200 --n-gpus 8      # 8×B200 (default), like the AEP
    ./run_app.sh --gpu H200 --n-gpus 8      # a cheaper tier to compare
    # run_app.sh -> setup_app.sh writes .app_tier.json + `modal deploy`s this, then benches the
    # resulting /v1 URL. The GPU/weights come from that file (this module reads no env except creds).

Auth: requires_proxy_auth=True, so the same Modal-Key/Modal-Secret headers bench.sh sends work.

⚠️  Deploy + smoke-test before trusting numbers: `--tp-size` follows --n-gpus, but some GPU counts
need arg changes (attention/MoE sharding), and the weights volume must actually hold the model
(--model-path / --weights-volume). The server args below are copied from the live AEP.
"""
import json
import os
import subprocess

import modal

SERVED_MODEL_NAME = "zai-org/GLM-5.2-FP8"
MODEL_REVISION = "a0b55e88465d1a06afece97bc8d6b366aff39089"   # pinned, matches the AEP
WEIGHTS_MOUNT = "/weights"

# Hardware tier + weights come from .app_tier.json, which setup_app.sh writes from its --gpu/--n-gpus
# flags right before `modal deploy` (modal deploy can't forward CLI args to this module, and we keep
# operational knobs out of env vars — only .env creds live in the environment). Defaults = the AEP's
# 8×B200. model_path default = the repo id (SGLang downloads into the mounted HF-cache volume on the
# first cold start, persists after); set it to a pre-populated volume path to skip the ~700 GB pull.
_tier = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "app_tier.json")) as _f:
        _tier = json.load(_f)
except Exception:
    pass
GPU_TYPE = _tier.get("gpu_type") or "B200"
N_GPUS = int(_tier.get("n_gpus") or 8)
GPU = f"{GPU_TYPE}:{N_GPUS}"
WEIGHTS_VOLUME = _tier.get("weights_volume") or "glm-app-hf-cache"
MODEL_PATH = _tier.get("model_path") or SERVED_MODEL_NAME

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

# SGLang server args. Most are the live AEP's GLM-5.2 config, but a few are ARCHITECTURE-SPECIFIC.
# The AEP runs on Blackwell (B200); its DSA attention uses the `trtllm` FMHA runner, which is
# Blackwell-only — on Hopper (H200/H100) it dies during cuda-graph capture ("TllmGenFmhaRunner ...
# Unsupported architecture"). CRUCIAL: SGLang auto-selects the trtllm DSA backend *because the KV
# cache is fp8_e4m3* ("Set DSA backends for fp8_e4m3 KV Cache: prefill=trtllm, decode=trtllm") — so
# just omitting --dsa-*-backend is NOT enough; we must also drop fp8 KV on Hopper so SGLang can pick
# a Hopper-safe DSA backend. Blackwell keeps the AEP-proven fp8-KV + trtllm config.
# ⚠️ Best-effort — smoke-test Hopper; if SGLang still forces trtllm, GLM-5.2 DSA may be Blackwell-only here.
_BLACKWELL = GPU_TYPE.upper().startswith(("B", "GB"))   # B200 / B100 / GB200

SERVER_ARGS = [
    "--served-model-name", SERVED_MODEL_NAME,
    "--chunked-prefill-size", "32768",
    "--disable-cuda-graph-padding",
    "--disable-piecewise-cuda-graph",
    "--dist-timeout", "3600",
    "--attention-backend", "dsa",         # GLM-5.2 needs DSA / MLA
    "--dsa-topk-backend", "sgl-kernel",
    "--fp8-gemm-backend", "deep_gemm",     # DeepGEMM runs on both Hopper and Blackwell
    "--reasoning-parser", "glm45",
    "--speculative-algorithm", "EAGLE",
    "--speculative-eagle-topk", "1",
    "--speculative-num-draft-tokens", "7",
    "--speculative-num-steps", "6",
    "--tool-call-parser", "glm47",
    "--trust-remote-code",
]
if _BLACKWELL:   # AEP-proven Blackwell config: fp8 KV + trtllm DSA
    SERVER_ARGS += ["--kv-cache-dtype", "fp8_e4m3",
                    "--dsa-decode-backend", "trtllm", "--dsa-prefill-backend", "trtllm",
                    "--cuda-graph-max-bs", "32", "--mem-fraction-static", "0.85"]
else:            # Hopper (H200/H100): NO trtllm FMHA (Blackwell-only) — let SGLang choose sub-backends
    SERVER_ARGS += ["--cuda-graph-max-bs", "16", "--mem-fraction-static", "0.80"]

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
