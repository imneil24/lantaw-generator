from unittest.mock import MagicMock, patch
from app.runpod_client import RunpodClient


def _fake_settings():
    return MagicMock(
        runpod_video_key="video-key",
        runpod_image_key="image-key",
        runpod_video_endpoint="https://api.runpod.ai/v2/vid/runsync",
        runpod_image_endpoint="https://api.runpod.ai/v2/img/runsync",
    )


@patch("app.runpod_client.httpx.post")
def test_dispatch_video_sends_only_prompt_and_duration(mock_post):
    mock_post.return_value = MagicMock(status_code=200, json=lambda: {"output": {"key": "clips/x.mp4"}})
    client = RunpodClient(_fake_settings())
    result = client.dispatch_video(prompt="a river at dawn", duration=8)
    assert result == {"output": {"key": "clips/x.mp4"}}
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.runpod.ai/v2/vid/runsync"
    assert kwargs["json"]["input"] == {"prompt": "a river at dawn", "duration": 8}
    assert kwargs["headers"]["Authorization"] == "Bearer video-key"


@patch("app.runpod_client.httpx.post")
def test_dispatch_image_sends_only_prompt(mock_post):
    mock_post.return_value = MagicMock(status_code=200, json=lambda: {"output": {"key": "images/x.png"}})
    client = RunpodClient(_fake_settings())
    result = client.dispatch_image(prompt="a red fox")
    assert result == {"output": {"key": "images/x.png"}}
    args, kwargs = mock_post.call_args
    assert kwargs["json"]["input"] == {"prompt": "a red fox"}
    assert kwargs["headers"]["Authorization"] == "Bearer image-key"


@patch("app.runpod_client.httpx.post")
def test_dispatch_video_raises_on_non_200(mock_post):
    mock_post.return_value = MagicMock(status_code=500, text="upstream error")
    client = RunpodClient(_fake_settings())
    try:
        client.dispatch_video(prompt="x", duration=5)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
