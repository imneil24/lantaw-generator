import base64
import os
import tempfile
import threading
import uuid

import torch

from ltx_core.model.video_vae import get_video_chunks_number
from ltx_pipelines.distilled import DistilledPipeline
from ltx_pipelines.utils.helpers import snap_frames_to_grid
from ltx_pipelines.utils.media_io import encode_video
from ltx_pipelines.utils.model_paths import ModelPaths
from ltx_pipelines.utils.types import OffloadMode

# Two-stage pipelines (DistilledPipeline) require both dimensions divisible by
# 64 (ltx_pipelines.utils.helpers.assert_resolution). 1088 is the standard
# "1080p-safe" height used across video codecs for exactly this reason
# (64 * 17 = 1088); true 1080 is not a multiple of 64.
RESOLUTION_WIDTH = 1920
RESOLUTION_HEIGHT = 1088
FPS = 24
FAST_MAX_DURATION = 20

MODEL_ROOT = "/runpod-volume/ltx-2.5"
TRANSFORMER_PATH = f"{MODEL_ROOT}/diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors"
TEXT_ENCODER_PATH = f"{MODEL_ROOT}/text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors"
VIDEO_VAE_PATH = f"{MODEL_ROOT}/vae/ltx-2.5-video-vae-bf16.safetensors"
AUDIO_VAE_PATH = f"{MODEL_ROOT}/vae/ltx-2.5-audio-vae-bf16.safetensors"
SPATIAL_UPSAMPLER_PATH = f"{MODEL_ROOT}/latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"

# Mirrors backend/app/moderation.py's DEFAULT_BLOCKLIST. The RunPod endpoint
# is reachable directly with its own Bearer key, independent of the backend
# proxy — this is a second, independent trust boundary, so moderation must
# be enforced here too, not only in the backend before enqueueing.
BLOCKLIST = [
    "child sexual", "csam", "bomb making", "how to build a bomb",
    "bioweapon", "chemical weapon synthesis",
]


def _is_blocked(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(term in lowered for term in BLOCKLIST)


_PIPELINE = None
_PIPELINE_LOCK = threading.Lock()


def _load_pipeline_impl() -> DistilledPipeline:
    model_paths = ModelPaths.from_split(
        transformer_path=TRANSFORMER_PATH,
        text_encoder_path=TEXT_ENCODER_PATH,
        video_vae_path=VIDEO_VAE_PATH,
        audio_vae_path=AUDIO_VAE_PATH,
    )
    return DistilledPipeline(
        model_paths=model_paths,
        spatial_upsampler_path=SPATIAL_UPSAMPLER_PATH,
        loras=[],
        # Default OffloadMode.NONE keeps every component (transformer, text
        # encoder, VAEs) resident on GPU at once — confirmed via a real OOM on
        # a 32GB card (~30GB allocated with zero headroom for computation).
        # CPU offloading trades some speed for the ~5GB VRAM / ~36GB RAM
        # footprint documented in ltx_pipelines.utils.types.OffloadMode.
        offload_mode=OffloadMode.CPU,
    )


def load_pipeline() -> DistilledPipeline:
    global _PIPELINE
    # RunPod serverless can dispatch concurrent requests to one warm worker
    # process. An unlocked check-then-act here would let two invocations
    # both see _PIPELINE is None and both run the GPU-weight-loading init
    # concurrently — wasted memory at best, corrupted shared state at worst.
    if _PIPELINE is None:
        with _PIPELINE_LOCK:
            if _PIPELINE is None:
                _PIPELINE = _load_pipeline_impl()
    return _PIPELINE


def validate_input(job_input: dict) -> tuple[str, float]:
    allowed_keys = {"prompt", "duration"}
    if set(job_input.keys()) - allowed_keys:
        raise ValueError(f"unexpected fields: {set(job_input.keys()) - allowed_keys}")
    if "prompt" not in job_input or not isinstance(job_input["prompt"], str) or not job_input["prompt"].strip():
        raise ValueError("prompt is required and must be a non-empty string")
    if "duration" not in job_input:
        raise ValueError("duration is required")
    duration = float(job_input["duration"])
    if not (0 < duration <= FAST_MAX_DURATION):
        raise ValueError(f"duration must be between 0 and {FAST_MAX_DURATION}")
    return job_input["prompt"], duration


def _generate_video(prompt: str, duration: float) -> bytes:
    pipeline = load_pipeline()
    # The VAE's causal temporal grid requires (frames - 1) % scale_factors.time == 0;
    # snap_frames_to_grid rounds down to the nearest valid value.
    num_frames = snap_frames_to_grid(round(duration * FPS))

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_file:
        output_path = tmp_file.name

    try:
        # LTX-2's own CLI entrypoint (ltx_pipelines.distilled.main) wraps the
        # whole pipeline call *and* the encode_video call in torch.inference_mode().
        # result.video is a lazy iterator: the actual VAE decode tensor ops run
        # when encode_video pulls from it, not when pipeline() returns — closing
        # the inference_mode context beforehand left that decode step running in
        # default (autograd-tracking) mode, which torch rejects when it touches
        # tensors created earlier under inference_mode: "Inference tensors cannot
        # be saved for backward."
        with torch.inference_mode():
            result = pipeline(
                prompt=prompt,
                seed=int.from_bytes(os.urandom(4), "big"),
                height=RESOLUTION_HEIGHT,
                width=RESOLUTION_WIDTH,
                frame_rate=FPS,
                images=[],
                num_frames=num_frames,
            )
            encode_video(
                video=result.video,
                fps=FPS,
                audio=result.audio,
                output_path=output_path,
                video_chunks_number=get_video_chunks_number(result.num_frames, result.tiling_config),
            )
        with open(output_path, "rb") as f:
            return f.read()
    finally:
        os.remove(output_path)


def handler(job: dict) -> dict:
    try:
        prompt, duration = validate_input(job.get("input", {}))
    except ValueError as e:
        return {"error": str(e)}

    if _is_blocked(prompt):
        return {"error": "prompt rejected by moderation"}

    video_bytes = _generate_video(prompt, duration)
    key = f"clips/{uuid.uuid4().hex}.mp4"
    return {"output": {"key": key, "bytes_b64": base64.b64encode(video_bytes).decode("ascii")}}
