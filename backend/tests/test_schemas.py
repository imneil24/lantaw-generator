import pytest
from pydantic import ValidationError
from app.schemas import GenerateImageRequest, GenerateVideoRequest


def test_generate_image_rejects_extra_fields():
    with pytest.raises(ValidationError):
        GenerateImageRequest(prompt="a cat", image_url="http://evil.example.com")


def test_generate_image_requires_prompt():
    with pytest.raises(ValidationError):
        GenerateImageRequest()


def test_generate_video_rejects_extra_fields():
    with pytest.raises(ValidationError):
        GenerateVideoRequest(prompt="a cat running", target_duration=30, resolution="4k")


def test_generate_video_duration_bounds():
    with pytest.raises(ValidationError):
        GenerateVideoRequest(prompt="a cat", target_duration=0)
    with pytest.raises(ValidationError):
        GenerateVideoRequest(prompt="a cat", target_duration=100000)


def test_generate_video_valid():
    req = GenerateVideoRequest(prompt="a cat running", target_duration=30)
    assert req.target_duration == 30
