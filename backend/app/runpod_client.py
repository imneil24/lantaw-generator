import time
from typing import Callable

import httpx


class RunpodClient:
    # 240 attempts * 5s = 20 minutes: real LTX-2.5 video generation (text
    # encode, transformer denoise, spatial upscale, VAE decode) has been
    # observed taking 8-13 minutes end to end. Dispatch itself no longer
    # polls against this ceiling (see dispatch_video/dispatch_image) — it
    # now bounds policy.executionTimeout and the reconciliation sweep's/
    # resume_orphaned_jobs's short capped polls instead.
    def __init__(self, settings, poll_interval: float = 5, max_poll_attempts: int = 240):
        self._video_key = settings.runpod_video_key
        self._image_key = settings.runpod_image_key
        self._video_endpoint = settings.runpod_video_endpoint
        self._image_endpoint = settings.runpod_image_endpoint
        self._public_base_url = settings.public_base_url
        self._webhook_secret = settings.runpod_webhook_secret
        self._poll_interval = poll_interval
        self._max_poll_attempts = max_poll_attempts

    def dispatch_video(self, prompt: str, duration: float, job_id: str,
                        on_submitted: Callable[[str], None] | None = None) -> dict:
        return self._dispatch(
            self._video_endpoint, self._video_key, {"prompt": prompt, "duration": duration}, job_id, on_submitted,
        )

    def dispatch_image(self, prompt: str, job_id: str,
                        on_submitted: Callable[[str], None] | None = None) -> dict:
        return self._dispatch(self._image_endpoint, self._image_key, {"prompt": prompt}, job_id, on_submitted)

    def poll_video(self, job_id: str, max_attempts: int | None = None) -> dict:
        """Polls an already-submitted video job by its RunPod job_id.

        Used by the webhook-delivery-fallback reconciliation sweep
        (ops/reconcile_stuck_jobs.py) and resume_orphaned_jobs — both need
        to check RunPod's status directly for a job whose webhook callback
        either hasn't arrived yet or was never delivered.

        max_attempts overrides the instance's default poll ceiling so
        these bounded safety-net sweeps don't block for the full ~20
        minute worst case on any single job.
        """
        base = self._video_endpoint.rsplit("/", 1)[0]
        return self._poll(base, self._video_key, job_id, max_attempts)

    def poll_image(self, job_id: str, max_attempts: int | None = None) -> dict:
        base = self._image_endpoint.rsplit("/", 1)[0]
        return self._poll(base, self._image_key, job_id, max_attempts)

    def _dispatch(self, runsync_endpoint: str, api_key: str, input_payload: dict,
                   job_id: str, on_submitted: Callable[[str], None] | None) -> dict:
        base = runsync_endpoint.rsplit("/", 1)[0]
        headers = {"Authorization": f"Bearer {api_key}"}
        webhook_url = f"{self._public_base_url}/webhooks/runpod/{self._webhook_secret}/{job_id}"

        # RunPod applies its own (undocumented, often shorter than expected)
        # default executionTimeout when a request doesn't specify one —
        # that default was killing jobs mid-run with a 400 on RunPod's own
        # /job-done callback even though the handler was still actively
        # generating, then reporting the failure as "executionTimeout
        # exceeded". Setting policy.executionTimeout (milliseconds)
        # explicitly overrides that default for this job. This is no
        # longer tied to our own poll loop (dispatch no longer polls) — it
        # remains sized off poll_interval/max_poll_attempts as a
        # known-good worst-case job duration ceiling.
        execution_timeout_ms = self._poll_interval * self._max_poll_attempts * 1000
        body = {
            "input": input_payload,
            "webhook": webhook_url,
            "policy": {"executionTimeout": execution_timeout_ms},
        }
        response = httpx.post(f"{base}/run", json=body, headers=headers, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"RunPod job submission failed: status={response.status_code}")
        runpod_job_id = response.json()["id"]
        if on_submitted is not None:
            on_submitted(runpod_job_id)

        return {"status": "dispatched", "runpod_job_id": runpod_job_id}

    def _poll(self, base: str, api_key: str, job_id: str, max_attempts: int | None = None) -> dict:
        attempts = self._max_poll_attempts if max_attempts is None else max_attempts
        headers = {"Authorization": f"Bearer {api_key}"}
        status_url = f"{base}/status/{job_id}"
        for _ in range(attempts):
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

        raise TimeoutError(f"RunPod job {job_id} did not complete within {attempts} poll attempts")
