from unittest.mock import MagicMock, patch
from app.runpod_client import RunpodClient


def _fake_settings():
    return MagicMock(
        runpod_video_key="video-key",
        runpod_image_key="image-key",
        runpod_video_endpoint="https://api.runpod.ai/v2/vid/runsync",
        runpod_image_endpoint="https://api.runpod.ai/v2/img/runsync",
    )


def _mock_response(status_code=200, body=None):
    return MagicMock(status_code=status_code, json=lambda: body, text=str(body))


@patch("app.runpod_client.httpx.get")
@patch("app.runpod_client.httpx.post")
def test_dispatch_video_submits_to_run_endpoint(mock_post, mock_get):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})
    mock_get.return_value = _mock_response(200, {"id": "job-1", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}})

    client = RunpodClient(_fake_settings())
    client.dispatch_video(prompt="a river at dawn", duration=8)

    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.runpod.ai/v2/vid/run"
    assert kwargs["json"]["input"] == {"prompt": "a river at dawn", "duration": 8}
    assert kwargs["headers"]["Authorization"] == "Bearer video-key"


@patch("app.runpod_client.httpx.get")
@patch("app.runpod_client.httpx.post")
def test_dispatch_video_polls_status_until_completed(mock_post, mock_get):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})
    mock_get.side_effect = [
        _mock_response(200, {"id": "job-1", "status": "IN_PROGRESS"}),
        _mock_response(200, {"id": "job-1", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}}),
    ]

    client = RunpodClient(_fake_settings(), poll_interval=0)
    result = client.dispatch_video(prompt="a river at dawn", duration=8)

    assert result == {"output": {"key": "clips/x.mp4"}}
    assert mock_get.call_count == 2
    status_args, status_kwargs = mock_get.call_args
    assert status_args[0] == "https://api.runpod.ai/v2/vid/status/job-1"
    assert status_kwargs["headers"]["Authorization"] == "Bearer video-key"


@patch("app.runpod_client.httpx.get")
@patch("app.runpod_client.httpx.post")
def test_dispatch_video_raises_on_failed_status(mock_post, mock_get):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})
    mock_get.return_value = _mock_response(200, {"id": "job-1", "status": "FAILED", "error": "OOM"})

    client = RunpodClient(_fake_settings(), poll_interval=0)
    try:
        client.dispatch_video(prompt="x", duration=5)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


@patch("app.runpod_client.httpx.get")
@patch("app.runpod_client.httpx.post")
def test_dispatch_video_raises_on_poll_timeout(mock_post, mock_get):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})
    mock_get.return_value = _mock_response(200, {"id": "job-1", "status": "IN_PROGRESS"})

    client = RunpodClient(_fake_settings(), poll_interval=0, max_poll_attempts=3)
    try:
        client.dispatch_video(prompt="x", duration=5)
        assert False, "expected TimeoutError"
    except TimeoutError:
        pass
    assert mock_get.call_count == 3


@patch("app.runpod_client.httpx.get")
@patch("app.runpod_client.httpx.post")
def test_dispatch_image_submits_and_polls(mock_post, mock_get):
    mock_post.return_value = _mock_response(200, {"id": "job-2", "status": "IN_QUEUE"})
    mock_get.return_value = _mock_response(200, {"id": "job-2", "status": "COMPLETED", "output": {"key": "images/x.png"}})

    client = RunpodClient(_fake_settings(), poll_interval=0)
    result = client.dispatch_image(prompt="a red fox")

    assert result == {"output": {"key": "images/x.png"}}
    post_args, post_kwargs = mock_post.call_args
    assert post_args[0] == "https://api.runpod.ai/v2/img/run"
    assert post_kwargs["json"]["input"] == {"prompt": "a red fox"}
    get_args, get_kwargs = mock_get.call_args
    assert get_args[0] == "https://api.runpod.ai/v2/img/status/job-2"


@patch("app.runpod_client.httpx.post")
def test_dispatch_video_raises_on_non_200_submit(mock_post):
    mock_post.return_value = _mock_response(500, "upstream error")
    client = RunpodClient(_fake_settings())
    try:
        client.dispatch_video(prompt="x", duration=5)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


@patch("app.runpod_client.httpx.get")
@patch("app.runpod_client.httpx.post")
def test_dispatch_video_passes_through_handler_error_on_completed_status(mock_post, mock_get):
    # RunPod status can be COMPLETED even when the handler itself rejected
    # the prompt (moderation/validation) rather than crashing — the handler's
    # own {"error": ...} return must survive un-wrapped so queue.py's
    # _extract_output sees the "error" key and raises a clean message
    # instead of a KeyError on a missing "bytes_b64".
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})
    mock_get.return_value = _mock_response(
        200, {"id": "job-1", "status": "COMPLETED", "output": {"error": "prompt rejected by moderation"}}
    )

    client = RunpodClient(_fake_settings(), poll_interval=0)
    result = client.dispatch_video(prompt="x", duration=5)

    assert result == {"error": "prompt rejected by moderation"}


@patch("app.runpod_client.httpx.get")
@patch("app.runpod_client.httpx.post")
def test_dispatch_video_calls_on_submitted_with_job_id_before_polling(mock_post, mock_get):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})
    mock_get.return_value = _mock_response(200, {"id": "job-1", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}})
    seen = []

    client = RunpodClient(_fake_settings(), poll_interval=0)
    client.dispatch_video(prompt="x", duration=5, on_submitted=seen.append)

    assert seen == ["job-1"]


@patch("app.runpod_client.httpx.post")
def test_dispatch_video_does_not_call_on_submitted_when_submit_fails(mock_post):
    mock_post.return_value = _mock_response(500, "upstream error")
    seen = []
    client = RunpodClient(_fake_settings())
    try:
        client.dispatch_video(prompt="x", duration=5, on_submitted=seen.append)
    except RuntimeError:
        pass
    assert seen == []
