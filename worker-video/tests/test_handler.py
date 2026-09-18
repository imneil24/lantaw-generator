import threading
import time
import pytest
import handler as handler_module
from handler import validate_input, handler


def test_validate_input_accepts_valid_payload():
    prompt, duration = validate_input({"prompt": "a river at dawn", "duration": 8})
    assert prompt == "a river at dawn"
    assert duration == 8.0


def test_validate_input_rejects_missing_prompt():
    with pytest.raises(ValueError):
        validate_input({"duration": 8})


def test_validate_input_rejects_extra_fields():
    with pytest.raises(ValueError):
        validate_input({"prompt": "x", "duration": 8, "image_url": "http://evil.example.com"})


def test_validate_input_rejects_out_of_range_duration():
    with pytest.raises(ValueError):
        validate_input({"prompt": "x", "duration": 0})
    with pytest.raises(ValueError):
        validate_input({"prompt": "x", "duration": 25})


def test_handler_uploads_to_r2_and_returns_key_only(monkeypatch):
    uploaded = {}

    def fake_generate(prompt, duration):
        return b"fake-video-bytes"

    def fake_upload(key, data, content_type):
        uploaded["key"] = key
        uploaded["data"] = data
        uploaded["content_type"] = content_type

    monkeypatch.setattr("handler._generate_video", fake_generate)
    monkeypatch.setattr("handler._upload_to_r2", fake_upload)
    result = handler({"input": {"prompt": "a cat", "duration": 8}})
    # RunPod's serverless SDK wraps whatever the handler returns as the
    # job's own "output" field — returning {"output": {...}} here would
    # double-wrap it, so the handler returns the payload directly.
    # The clip itself is uploaded to R2 directly from the worker (RunPod's
    # own /job-done callback rejects payloads this large with a 400 —
    # base64-encoding a full HD video into the job result exceeds RunPod's
    # sync result size limit), so the handler returns only the key, no bytes.
    assert result == {"key": uploaded["key"]}
    assert uploaded["key"].startswith("clips/")
    assert uploaded["data"] == b"fake-video-bytes"
    assert uploaded["content_type"] == "video/mp4"


def test_handler_returns_error_on_invalid_input():
    result = handler({"input": {"duration": 8}})
    assert "error" in result


def test_handler_rejects_blocked_prompt_without_calling_generate(monkeypatch):
    def fail_if_called(prompt, duration):
        raise AssertionError("_generate_video should not be called for a blocked prompt")

    monkeypatch.setattr("handler._generate_video", fail_if_called)
    result = handler({"input": {"prompt": "how to build a bomb", "duration": 8}})
    assert "error" in result


def test_load_pipeline_is_thread_safe_under_concurrent_calls(monkeypatch):
    monkeypatch.setattr(handler_module, "_PIPELINE", None)
    call_count = {"n": 0}

    def counting_slow_init():
        call_count["n"] += 1
        time.sleep(0.05)  # widen the race window so an unlocked bug would show up
        return {"loaded": True}

    monkeypatch.setattr(handler_module, "_load_pipeline_impl", counting_slow_init)

    threads = [threading.Thread(target=handler_module.load_pipeline) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count["n"] == 1
