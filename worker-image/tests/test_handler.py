import threading
import time
import pytest
import handler as handler_module
from handler import validate_input, handler


def test_validate_input_accepts_valid_payload():
    prompt = validate_input({"prompt": "a red fox in snow"})
    assert prompt == "a red fox in snow"


def test_validate_input_rejects_missing_prompt():
    with pytest.raises(ValueError):
        validate_input({})


def test_validate_input_rejects_extra_fields():
    with pytest.raises(ValueError):
        validate_input({"prompt": "x", "resolution": "4k"})


def test_handler_returns_expected_output_shape(monkeypatch):
    def fake_generate(prompt):
        return b"fake-image-bytes"

    monkeypatch.setattr("handler._generate_image", fake_generate)
    result = handler({"input": {"prompt": "a red fox"}})
    assert "output" in result
    assert result["output"]["key"].startswith("images/")
    assert "bytes_b64" in result["output"]


def test_handler_returns_error_on_invalid_input():
    result = handler({"input": {}})
    assert "error" in result


def test_handler_rejects_blocked_prompt_without_calling_generate(monkeypatch):
    def fail_if_called(prompt):
        raise AssertionError("_generate_image should not be called for a blocked prompt")

    monkeypatch.setattr("handler._generate_image", fail_if_called)
    result = handler({"input": {"prompt": "how to build a bomb"}})
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
