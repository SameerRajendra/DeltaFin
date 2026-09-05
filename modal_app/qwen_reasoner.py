"""Serverless Qwen2.5-7B-Instruct reasoning endpoint, scale-to-zero on Modal.

Deploy:
    modal deploy modal_app/qwen_reasoner.py

Then copy the printed https://...modal.run URL into MODAL_QWEN_URL in .env.
The endpoint requires Modal proxy auth (Modal-Key/Modal-Secret, or the combined
"Authorization: Bearer <key>.<secret>" form) -- callers must send the same
credential pair issued in the Modal dashboard and already stored in .env as
MODAL_KEY / MODAL_SECRET.

No min_containers is set, so idle containers scale to zero between requests;
the tradeoff is a cold start (~model load time) on the first request after a
period of inactivity. Model weights persist in a Modal Volume across restarts
so a cold start re-downloads nothing.
"""

import modal

MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"
SERVED_MODEL_NAME = "qwen-reasoner"

app = modal.App("qwen-reasoner")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "vllm==0.6.3",
        "huggingface_hub[hf_transfer]==0.25.2",
    )
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
    # vLLM's guided-decoding path unconditionally imports outlines, which
    # unconditionally imports "pyairports" for an airport-code enum type we
    # never use. The only version on PyPI (0.0.1) is an abandoned placeholder
    # whose sdist ships no actual `pyairports` package -- so it 500s on the
    # very first chat completion regardless of request content. An empty
    # AIRPORT_LIST is a legal input to outlines' Enum(...) construction, so a
    # tiny stub module is a complete, correct fix, not a workaround.
    .run_commands(
        "mkdir -p /usr/local/lib/python3.11/site-packages/pyairports",
        "printf '' > /usr/local/lib/python3.11/site-packages/pyairports/__init__.py",
        "printf 'AIRPORT_LIST = []\\n' > /usr/local/lib/python3.11/site-packages/pyairports/airports.py",
    )
)

hf_cache = modal.Volume.from_name("qwen-hf-cache", create_if_missing=True)


@app.function(
    image=image,
    gpu="A10G",
    scaledown_window=180,
    timeout=600,
    volumes={"/root/.cache/huggingface": hf_cache},
)
@modal.web_server(port=8000, startup_timeout=600, requires_proxy_auth=True)
def serve():
    import subprocess

    subprocess.Popen(
        " ".join(
            [
                "vllm serve",
                MODEL_NAME,
                "--host 0.0.0.0",
                "--port 8000",
                f"--served-model-name {SERVED_MODEL_NAME}",
                "--max-model-len 8192",
                "--gpu-memory-utilization 0.90",
            ]
        ),
        shell=True,
    )
