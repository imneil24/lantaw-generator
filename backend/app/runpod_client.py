import time

import httpx


class RunpodClient:
    def __init__(self, settings, poll_interval: float = 5, max_poll_attempts: int = 120):
        self._video_key = settings.runpod_video_key
        self._image_key = settings.runpod_image_key
        self._video_endpoint = settings.runpod_video_endpoint
        self._image_endpoint = settings.runpod_image_endpoint
        self._poll_interval = poll_interval
        self._max_poll_attempts = max_poll_attempts

    def dispatch_video(self, prompt: str, duration: float) -> dict:
        return self._dispatch(
            self._video_endpoint, self._video_key, {"prompt": prompt, "duration": duration}
        )

    def dispatch_image(self, prompt: str) -> dict:
        return self._dispatch(self._image_endpoint, self._image_key, {"prompt": prompt})

    def _dispatch(self, runsync_endpoint: str, api_key: str, input_payload: dict) -> dict:
        base = runsync_endpoint.rsplit("/", 1)[0]
        headers = {"Authorization": f"Bearer {api_key}"}

        response = httpx.post(f"{base}/run", json={"input": input_payload}, headers=headers, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"RunPod job submission failed: status={response.status_code}")
        job_id = response.json()["id"]

        status_url = f"{base}/status/{job_id}"
        for _ in range(self._max_poll_attempts):
            status_response = httpx.get(status_url, headers=headers, timeout=30)
            if status_response.status_code != 200:
                raise RuntimeError(f"RunPod status check failed: status={status_response.status_code}")
            payload = status_response.json()
            status = payload["status"]
            if status == "COMPLETED":
                return {"output": payload["output"]}
            if status == "FAILED":
                raise RuntimeError(f"RunPod job failed: {payload.get('error', payload)}")
            time.sleep(self._poll_interval)

        raise TimeoutError(f"RunPod job {job_id} did not complete within {self._max_poll_attempts} poll attempts")
