from unittest.mock import MagicMock, patch
from app.runpod_client import RunpodClient


def _fake_settings():
    return MagicMock(
        runpod_video_key="video-key",
        runpod_image_key="image-key",
        runpod_video_endpoint="https://api.runpod.ai/v2/vid/runsync",
        runpod_image_endpoint="https://api.runpod.ai/v2/img/runsync",
        public_base_url="https://api.example.com",
        runpod_webhook_secret="s3cr3t",
    )


def _mock_response(status_code=200, body=None):
    return MagicMock(status_code=status_code, json=lambda: body, text=str(body))


def test_default_poll_ceiling_covers_observed_generation_time():
    # Real LTX-2.5 generation has been observed taking 8-13 minutes; the
    # default poll ceiling must clear that with margin so our own client
    # doesn't time out jobs that are still running fine on RunPod's side.
    client = RunpodClient(_fake_settings())
    assert client._poll_interval * client._max_poll_attempts >= 15 * 60


@patch("app.runpod_client.httpx.post")
def test_dispatch_video_sets_execution_timeout_policy_matching_poll_ceiling(mock_post):
    # RunPod applies its own (undocumented, often too short) default
    # executionTimeout when a request doesn't specify one, which was
    # killing jobs mid-run with a 400 on RunPod's own /job-done callback
    # even though the handler was still actively generating — RunPod then
    # reports that as "executionTimeout exceeded". Setting policy.executionTimeout
    # explicitly, in milliseconds, overrides RunPod's default for this job.
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})

    client = RunpodClient(_fake_settings())
    client.dispatch_video(prompt="a river at dawn", duration=8, job_id="j")

    args, kwargs = mock_post.call_args
    expected_ms = client._poll_interval * client._max_poll_attempts * 1000
    assert kwargs["json"]["policy"]["executionTimeout"] == expected_ms


@patch("app.runpod_client.httpx.post")
def test_dispatch_video_submits_to_run_endpoint(mock_post):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})

    client = RunpodClient(_fake_settings())
    client.dispatch_video(prompt="a river at dawn", duration=8, job_id="j")

    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.runpod.ai/v2/vid/run"
    assert kwargs["json"]["input"] == {"prompt": "a river at dawn", "duration": 8}
    assert kwargs["headers"]["Authorization"] == "Bearer video-key"
    assert "webhook" in kwargs["json"]


@patch("app.runpod_client.httpx.post")
def test_dispatch_video_includes_webhook_url_scoped_to_the_job(mock_post):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})

    client = RunpodClient(_fake_settings())
    client.dispatch_video(prompt="a river at dawn", duration=8, job_id="our-job-42")

    args, kwargs = mock_post.call_args
    assert kwargs["json"]["webhook"] == "https://api.example.com/webhooks/runpod/s3cr3t/our-job-42"


@patch("app.runpod_client.httpx.get")
@patch("app.runpod_client.httpx.post")
def test_dispatch_video_returns_immediately_without_polling(mock_post, mock_get):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})

    client = RunpodClient(_fake_settings())
    result = client.dispatch_video(prompt="a river at dawn", duration=8, job_id="our-job-42")

    assert result == {"status": "dispatched", "runpod_job_id": "job-1"}
    mock_get.assert_not_called()


@patch("app.runpod_client.httpx.post")
def test_dispatch_image_submits_and_returns_dispatched(mock_post):
    mock_post.return_value = _mock_response(200, {"id": "job-2", "status": "IN_QUEUE"})

    client = RunpodClient(_fake_settings())
    result = client.dispatch_image(prompt="a red fox", job_id="our-job-99")

    assert result == {"status": "dispatched", "runpod_job_id": "job-2"}
    post_args, post_kwargs = mock_post.call_args
    assert post_args[0] == "https://api.runpod.ai/v2/img/run"
    assert post_kwargs["json"]["input"] == {"prompt": "a red fox"}
    assert post_kwargs["json"]["webhook"] == "https://api.example.com/webhooks/runpod/s3cr3t/our-job-99"


@patch("app.runpod_client.httpx.post")
def test_dispatch_video_raises_on_non_200_submit(mock_post):
    mock_post.return_value = _mock_response(500, "upstream error")
    client = RunpodClient(_fake_settings())
    try:
        client.dispatch_video(prompt="x", duration=5, job_id="j")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


@patch("app.runpod_client.httpx.post")
def test_dispatch_video_calls_on_submitted_with_job_id_before_returning(mock_post):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})
    seen = []

    client = RunpodClient(_fake_settings())
    client.dispatch_video(prompt="x", duration=5, job_id="j", on_submitted=seen.append)

    assert seen == ["job-1"]


@patch("app.runpod_client.httpx.post")
def test_dispatch_video_does_not_call_on_submitted_when_submit_fails(mock_post):
    mock_post.return_value = _mock_response(500, "upstream error")
    seen = []
    client = RunpodClient(_fake_settings())
    try:
        client.dispatch_video(prompt="x", duration=5, job_id="j", on_submitted=seen.append)
    except RuntimeError:
        pass
    assert seen == []
