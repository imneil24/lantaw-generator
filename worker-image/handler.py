import base64
import threading
import uuid

RESOLUTION_WIDTH = 1920
RESOLUTION_HEIGHT = 1080
MODEL_VARIANT = "FLUX.1-schnell"

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

_MODEL = None
_MODEL_LOCK = threading.Lock()


def _load_model_impl():
    # Real FLUX.1-schnell weight load from the attached Network Volume
    # happens here. Integration point only — inference call not vendored.
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


def validate_input(job_input: dict) -> str:
    allowed_keys = {"prompt"}
    if set(job_input.keys()) - allowed_keys:
        raise ValueError(f"unexpected fields: {set(job_input.keys()) - allowed_keys}")
    if "prompt" not in job_input or not isinstance(job_input["prompt"], str) or not job_input["prompt"].strip():
        raise ValueError("prompt is required and must be a non-empty string")
    return job_input["prompt"]


def _generate_image(prompt: str) -> bytes:
    load_model()
    raise NotImplementedError("wire actual FLUX.1-schnell inference call here")


def handler(job: dict) -> dict:
    try:
        prompt = validate_input(job.get("input", {}))
    except ValueError as e:
        return {"error": str(e)}

    if _is_blocked(prompt):
        return {"error": "prompt rejected by moderation"}

    image_bytes = _generate_image(prompt)
    key = f"images/{uuid.uuid4().hex}.png"
    return {"key": key, "bytes_b64": base64.b64encode(image_bytes).decode("ascii")}
