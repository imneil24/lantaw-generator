import base64
import os
import tempfile
import threading
import uuid

from ltx_core.model.video_vae import get_video_chunks_number
from ltx_pipelines.distilled import DistilledPipeline
from ltx_pipelines.utils.media_io import encode_video
from ltx_pipelines.utils.model_paths import ModelPaths

RESOLUTION_WIDTH = 1920
RESOLUTION_HEIGHT = 1080
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
    num_frames = round(duration * FPS)

    result = pipeline(
        prompt=prompt,
        seed=int.from_bytes(os.urandom(4), "big"),
        height=RESOLUTION_HEIGHT,
        width=RESOLUTION_WIDTH,
        frame_rate=FPS,
        images=[],
        num_frames=num_frames,
    )

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_file:
        output_path = tmp_file.name

    try:
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
