import threading
import time
import pytest
import handler as handler_module
from handler import validate_input, select_model_variant, handler


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


def test_select_model_variant_routes_by_duration():
    assert select_model_variant(10) == "ltx-2-3-pro"
    assert select_model_variant(10.0) == "ltx-2-3-pro"
    assert select_model_variant(15) == "ltx-2-3-fast"
    assert select_model_variant(20) == "ltx-2-3-fast"


def test_handler_returns_expected_output_shape(monkeypatch):
    def fake_generate(prompt, duration, variant):
        return b"fake-video-bytes"

    monkeypatch.setattr("handler._generate_video", fake_generate)
    result = handler({"input": {"prompt": "a cat", "duration": 8}})
    # RunPod's serverless SDK wraps whatever the handler returns as the
    # job's own "output" field — returning {"output": {...}} here would
    # double-wrap it, so the handler returns the payload directly.
    assert "key" in result
    assert result["key"].startswith("clips/")
    assert "bytes_b64" in result


def test_handler_returns_error_on_invalid_input():
    result = handler({"input": {"duration": 8}})
    assert "error" in result


def test_handler_rejects_blocked_prompt_without_calling_generate(monkeypatch):
    def fail_if_called(prompt, duration, variant):
        raise AssertionError("_generate_video should not be called for a blocked prompt")

    monkeypatch.setattr("handler._generate_video", fail_if_called)
    result = handler({"input": {"prompt": "how to build a bomb", "duration": 8}})
    assert "error" in result


def test_load_model_is_thread_safe_under_concurrent_calls(monkeypatch):
    monkeypatch.setattr(handler_module, "_MODEL", None)
    call_count = {"n": 0}

    def counting_slow_init():
        call_count["n"] += 1
        time.sleep(0.05)  # widen the race window so an unlocked bug would show up
        return {"loaded": True}

    monkeypatch.setattr(handler_module, "_load_model_impl", counting_slow_init)

    threads = [threading.Thread(target=handler_module.load_model) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count["n"] == 1
