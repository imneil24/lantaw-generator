import httpx


class RunpodClient:
    def __init__(self, settings):
        self._video_key = settings.runpod_video_key
        self._image_key = settings.runpod_image_key
        self._video_endpoint = settings.runpod_video_endpoint
        self._image_endpoint = settings.runpod_image_endpoint

    def dispatch_video(self, prompt: str, duration: float) -> dict:
        response = httpx.post(
            self._video_endpoint,
            json={"input": {"prompt": prompt, "duration": duration}},
            headers={"Authorization": f"Bearer {self._video_key}"},
            timeout=120,
        )
        if response.status_code != 200:
            raise RuntimeError(f"RunPod video dispatch failed: status={response.status_code}")
        return response.json()

    def dispatch_image(self, prompt: str) -> dict:
        response = httpx.post(
            self._image_endpoint,
            json={"input": {"prompt": prompt}},
            headers={"Authorization": f"Bearer {self._image_key}"},
            timeout=60,
        )
        if response.status_code != 200:
            raise RuntimeError(f"RunPod image dispatch failed: status={response.status_code}")
        return response.json()
