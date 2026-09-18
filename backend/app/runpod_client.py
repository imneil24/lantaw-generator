import time
from typing import Callable

import httpx


class RunpodClient:
    # 240 attempts * 5s = 20 minutes: real LTX-2.5 video generation (text
    # encode, transformer denoise, spatial upscale, VAE decode) has been
    # observed taking 8-13 minutes end to end, so the previous 10-minute
    # ceiling raced RunPod's own executionTimeout and lost, aborting jobs
    # that were still running fine on RunPod's side.
    def __init__(self, settings, poll_interval: float = 5, max_poll_attempts: int = 240):
        self._video_key = settings.runpod_video_key
        self._image_key = settings.runpod_image_key
        self._video_endpoint = settings.runpod_video_endpoint
        self._image_endpoint = settings.runpod_image_endpoint
        self._poll_interval = poll_interval
        self._max_poll_attempts = max_poll_attempts

    def dispatch_video(self, prompt: str, duration: float, on_submitted: Callable[[str], None] | None = None) -> dict:
        return self._dispatch(
            self._video_endpoint, self._video_key, {"prompt": prompt, "duration": duration}, on_submitted,
        )

    def dispatch_image(self, prompt: str, on_submitted: Callable[[str], None] | None = None) -> dict:
        return self._dispatch(self._image_endpoint, self._image_key, {"prompt": prompt}, on_submitted)

    def poll_video(self, job_id: str) -> dict:
        """Resumes polling an already-submitted video job by its RunPod job_id.

        For a job whose runpod_job_id was persisted (see queue.py's
        on_submitted callback) but whose worker process died before the
        original dispatch_video's poll loop returned — resuming here polls
        the same RunPod job instead of submitting a duplicate one.
        """
        base = self._video_endpoint.rsplit("/", 1)[0]
        return self._poll(base, self._video_key, job_id)

    def poll_image(self, job_id: str) -> dict:
        base = self._image_endpoint.rsplit("/", 1)[0]
        return self._poll(base, self._image_key, job_id)

    def _dispatch(self, runsync_endpoint: str, api_key: str, input_payload: dict,
                   on_submitted: Callable[[str], None] | None) -> dict:
        base = runsync_endpoint.rsplit("/", 1)[0]
        headers = {"Authorization": f"Bearer {api_key}"}

        # RunPod applies its own (undocumented, often shorter than expected)
        # default executionTimeout when a request doesn't specify one —
        # that default was killing jobs mid-run with a 400 on RunPod's own
        # /job-done callback even though the handler was still actively
        # generating, then reporting the failure as "executionTimeout
        # exceeded". Setting policy.executionTimeout (milliseconds)
        # explicitly, matching our own poll ceiling, overrides that default
        # for this job so RunPod's own timeout can't fire before ours does.
        execution_timeout_ms = self._poll_interval * self._max_poll_attempts * 1000
        body = {"input": input_payload, "policy": {"executionTimeout": execution_timeout_ms}}
        response = httpx.post(f"{base}/run", json=body, headers=headers, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"RunPod job submission failed: status={response.status_code}")
        job_id = response.json()["id"]
        if on_submitted is not None:
            on_submitted(job_id)

        return self._poll(base, api_key, job_id)

    def _poll(self, base: str, api_key: str, job_id: str) -> dict:
        headers = {"Authorization": f"Bearer {api_key}"}
        status_url = f"{base}/status/{job_id}"
        for _ in range(self._max_poll_attempts):
            status_response = httpx.get(status_url, headers=headers, timeout=30)
            if status_response.status_code != 200:
                raise RuntimeError(f"RunPod status check failed: status={status_response.status_code}")
            payload = status_response.json()
            status = payload["status"]
            if status == "COMPLETED":
                handler_output = payload["output"]
                if isinstance(handler_output, dict) and "error" in handler_output:
                    return handler_output
                return {"output": handler_output}
            if status == "FAILED":
                raise RuntimeError(f"RunPod job failed: {payload.get('error', payload)}")
            time.sleep(self._poll_interval)

        raise TimeoutError(f"RunPod job {job_id} did not complete within {self._max_poll_attempts} poll attempts")
