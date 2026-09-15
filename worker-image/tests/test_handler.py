import pytest
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
