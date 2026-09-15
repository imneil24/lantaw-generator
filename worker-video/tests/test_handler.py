import pytest
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
    assert "output" in result
    assert "key" in result["output"]
    assert result["output"]["key"].startswith("clips/")
    assert "bytes_b64" in result["output"]


def test_handler_returns_error_on_invalid_input():
    result = handler({"input": {"duration": 8}})
    assert "error" in result
