import base64
import threading
import uuid

RESOLUTION = "1080p"
FPS = 24
PRO_MAX_DURATION = 10
FAST_MAX_DURATION = 20

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

_MODEL = None  # loaded once at container start, see load_model()
_MODEL_LOCK = threading.Lock()


def _load_model_impl():
    # Real model load happens here (LTX-2.3 weights from the attached
    # Network Volume). Left as an integration point for the actual
    # LTX-2.3 runtime — this plan does not vendor model-loading code.
    return {"loaded": True}


def load_model():
    global _MODEL
    # RunPod serverless can dispatch concurrent requests to one warm worker
    # process. An unlocked check-then-act here would let two invocations
    # both see _MODEL is None and both run the (eventually GPU-weight-
    # loading) init concurrently — wasted memory at best, corrupted shared
    # state at worst once _load_model_impl is a real model load.
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                _MODEL = _load_model_impl()
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

    if _is_blocked(prompt):
        return {"error": "prompt rejected by moderation"}

    variant = select_model_variant(duration)
    video_bytes = _generate_video(prompt, duration, variant)
    key = f"clips/{uuid.uuid4().hex}.mp4"
    return {"output": {"key": key, "bytes_b64": base64.b64encode(video_bytes).decode("ascii")}}
