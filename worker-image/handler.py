import base64
import uuid

RESOLUTION_WIDTH = 1920
RESOLUTION_HEIGHT = 1080
MODEL_VARIANT = "FLUX.1-schnell"

_MODEL = None


def load_model():
    global _MODEL
    if _MODEL is None:
        # Real FLUX.1-schnell weight load from the attached Network Volume
        # happens here. Integration point only — inference call not vendored.
        _MODEL = {"loaded": True}
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

    image_bytes = _generate_image(prompt)
    key = f"images/{uuid.uuid4().hex}.png"
    return {"output": {"key": key, "bytes_b64": base64.b64encode(image_bytes).decode("ascii")}}
