import base64
import uuid

RESOLUTION = "1080p"
FPS = 24
PRO_MAX_DURATION = 10
FAST_MAX_DURATION = 20

_MODEL = None  # loaded once at container start, see load_model()


def load_model():
    global _MODEL
    if _MODEL is None:
        # Real model load happens here (LTX-2.3 weights from the attached
        # Network Volume). Left as an integration point for the actual
        # LTX-2.3 runtime — this plan does not vendor model-loading code.
        _MODEL = {"loaded": True}
    return _MODEL


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


def select_model_variant(duration: float) -> str:
    return "ltx-2-3-pro" if duration <= PRO_MAX_DURATION else "ltx-2-3-fast"


def _generate_video(prompt: str, duration: float, variant: str) -> bytes:
    load_model()
    raise NotImplementedError("wire actual LTX-2.3 inference call here")


def handler(job: dict) -> dict:
    try:
        prompt, duration = validate_input(job.get("input", {}))
    except ValueError as e:
        return {"error": str(e)}

    variant = select_model_variant(duration)
    video_bytes = _generate_video(prompt, duration, variant)
    key = f"clips/{uuid.uuid4().hex}.mp4"
    return {"output": {"key": key, "bytes_b64": base64.b64encode(video_bytes).decode("ascii")}}
