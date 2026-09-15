# LTX-2.3 Video + FLUX.1 Image Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a secure backend proxy + two RunPod Serverless workers (LTX-2.3 video, FLUX.1-schnell image) exposing minimal `prompt`(+`duration`) APIs, with full perimeter/secret/abuse hardening, a job queue, stitching pipeline, and operational guardrail scripts.

**Architecture:** FastAPI backend (auth, rate-limit, moderation, Postgres job tracking, Redis+RQ queue) dispatches to two independent RunPod Serverless endpoints; clip/image outputs land in Cloudflare R2; an RQ stitcher worker concatenates completed video clips with ffmpeg and muxes a stub silent audio track; standalone `ops/` cron scripts guard cost and RunPod scale-down drift.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, RQ + Redis, PostgreSQL (SQLAlchemy 2.0), boto3 (R2 is S3-compatible), ffmpeg-python, pytest, RunPod Python SDK, Docker + docker-compose, Caddy (TLS).

**Spec:** [docs/superpowers/specs/2026-09-15-ltx-video-flux-image-service-design.md](../specs/2026-09-15-ltx-video-flux-image-service-design.md)

## Global Constraints

- Public API surface is `prompt`+`duration` (video) or `prompt` (image) ONLY — every Pydantic request model uses `model_config = ConfigDict(extra="forbid")`.
- No URL-typed field anywhere in any public request schema or worker handler input (no `image_url`) — SSRF surface must not exist.
- Hardcoded server-side, never caller-supplied: video resolution 1080p, fps 24; image resolution 1920x1080.
- Video duration bounds: `1 <= duration <= 20`; `duration <= 10` routes to `ltx-2-3-pro`, else `ltx-2-3-fast`.
- Image model: FLUX.1-schnell only (Apache 2.0) — never FLUX.1-dev.
- All secret values (RunPod keys x2, R2 keys x2, Postgres DSN, Redis DSN, webhook URL) come from environment variables only; never hardcoded, never logged in plaintext.
- API key comparison uses `hmac.compare_digest`, never `==`.
- All job/project IDs are UUIDv4.
- Error responses to callers never include stack traces or upstream error detail — only a correlation ID; full detail logged server-side only.
- TTS is a stub (`NullTTSProvider`) in this plan — no real TTS provider integration.

---

## File Structure

```
backend/
  app/
    __init__.py
    config.py              # env-var settings (pydantic-settings)
    logging_conf.py        # secret-redacting logging filter
    security.py            # API key hashing/verification, hmac compare
    db.py                  # SQLAlchemy engine/session
    models.py              # ORM: Job, VideoProject, ProjectClip
    schemas.py             # Pydantic request/response models
    moderation.py          # ModerationProvider interface + keyword impl
    r2.py                  # R2 client wrapper (scoped read/write keys)
    runpod_client.py       # RunPod dispatch client (video + image)
    tts.py                 # TTSProvider interface + NullTTSProvider
    stitcher.py            # ffmpeg concat + audio mux
    queue.py               # RQ queue setup + job functions
    main.py                # FastAPI app, routes, middleware wiring
    routes/
      __init__.py
      generate_image.py    # POST /generate-image
      generate_video.py    # POST /generate-video
      jobs.py               # GET /jobs/{id}, GET /projects/{id}
    middleware/
      __init__.py
      auth.py               # Bearer key auth dependency
      rate_limit.py         # Redis sliding-window limiter
  tests/
    test_security.py
    test_schemas.py
    test_moderation.py
    test_r2.py
    test_runpod_client.py
    test_tts.py
    test_stitcher.py
    test_rate_limit.py
    test_routes_image.py
    test_routes_video.py
    test_routes_jobs.py
  Dockerfile
  requirements.txt
  .env.example

worker-video/
  handler.py
  Dockerfile
  requirements.txt
  tests/
    test_handler.py

worker-image/
  handler.py
  Dockerfile
  requirements.txt
  tests/
    test_handler.py

ops/
  cost_alert.py
  scale_guard.py
  revenue_tracker.py
  tests/
    test_cost_alert.py
    test_scale_guard.py
    test_revenue_tracker.py

docker-compose.yml
Caddyfile
.gitignore
```

**Interface contracts locked across tasks:**
- `security.verify_api_key(provided: str, stored_hash: str) -> bool`
- `security.hash_api_key(raw: str) -> str`
- `moderation.ModerationProvider.check(prompt: str) -> ModerationResult` where `ModerationResult` has `.allowed: bool` and `.reason: str | None`
- `r2.R2Client.upload(key: str, data: bytes, content_type: str) -> str` (returns object key), `r2.R2Client.signed_url(key: str, expires_in: int = 3600) -> str`
- `runpod_client.RunpodClient.dispatch_video(prompt: str, duration: float) -> dict`, `runpod_client.RunpodClient.dispatch_image(prompt: str) -> dict`
- `tts.TTSProvider.generate(text: str, target_duration: float) -> str` (returns local file path to audio)
- `stitcher.stitch_project(project_id: str, clip_paths: list[str], audio_path: str, output_path: str) -> None`
- `models.Job(id, api_key_id, type, prompt, duration, status, retry_count, result_key, created_at)`
- `models.VideoProject(id, api_key_id, target_duration, clip_count, status, final_result_key)`
- `models.ProjectClip(project_id, sequence_index, job_id)`

---

## Task 1: Project Scaffolding + Config + Secret-Redacting Logging

**Files:**
- Create: `backend/app/__init__.py`, `backend/app/config.py`, `backend/app/logging_conf.py`
- Create: `backend/requirements.txt`, `backend/.env.example`, `.gitignore`
- Test: `backend/tests/test_logging_conf.py`

**Interfaces:**
- Produces: `config.Settings` (pydantic-settings `BaseSettings` subclass) with fields: `runpod_video_key: str`, `runpod_image_key: str`, `runpod_video_endpoint: str`, `runpod_image_endpoint: str`, `r2_write_key: str`, `r2_write_secret: str`, `r2_read_key: str`, `r2_read_secret: str`, `r2_bucket: str`, `r2_endpoint: str`, `postgres_dsn: str`, `redis_url: str`, `backend_api_key_hash: str`, `webhook_url: str`, `moderation_api_key: str | None = None`. Produces `config.get_settings() -> Settings` (cached via `functools.lru_cache`).
- Produces: `logging_conf.configure_logging(secrets: list[str]) -> None` — installs a `logging.Filter` on the root logger that replaces any occurrence of each secret string in a `LogRecord`'s formatted message with `"[REDACTED]"`.

- [ ] **Step 1: Write the failing test for the redacting filter**

```python
# backend/tests/test_logging_conf.py
import logging
from app.logging_conf import configure_logging

def test_redacts_secret_in_log_message(caplog):
    configure_logging(secrets=["super-secret-value"])
    logger = logging.getLogger("test.redact")
    with caplog.at_level(logging.INFO):
        logger.info("token=super-secret-value used")
    assert "super-secret-value" not in caplog.text
    assert "[REDACTED]" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_logging_conf.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'` or `ImportError`

- [ ] **Step 3: Write `backend/app/__init__.py` (empty) and `backend/app/logging_conf.py`**

```python
# backend/app/logging_conf.py
import logging


class _RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            if secret in msg:
                msg = msg.replace(secret, "[REDACTED]")
        record.msg = msg
        record.args = ()
        return True


def configure_logging(secrets: list[str]) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for f in list(root.filters):
        root.removeFilter(f)
    root.addFilter(_RedactFilter(secrets))
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_logging_conf.py -v`
Expected: PASS

- [ ] **Step 5: Write `backend/app/config.py`**

```python
# backend/app/config.py
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    runpod_video_key: str
    runpod_image_key: str
    runpod_video_endpoint: str
    runpod_image_endpoint: str
    r2_write_key: str
    r2_write_secret: str
    r2_read_key: str
    r2_read_secret: str
    r2_bucket: str
    r2_endpoint: str
    postgres_dsn: str
    redis_url: str
    backend_api_key_hash: str
    webhook_url: str
    moderation_api_key: str | None = None

    def all_secrets(self) -> list[str]:
        return [
            self.runpod_video_key, self.runpod_image_key,
            self.r2_write_secret, self.r2_read_secret,
            self.postgres_dsn, self.redis_url,
            self.backend_api_key_hash, self.webhook_url,
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 6: Write `backend/requirements.txt`**

```
fastapi==0.115.0
uvicorn[standard]==0.32.0
pydantic==2.9.2
pydantic-settings==2.5.2
sqlalchemy==2.0.35
psycopg2-binary==2.9.9
redis==5.1.0
rq==2.0.0
boto3==1.35.36
ffmpeg-python==0.2.0
runpod==1.7.4
bcrypt==4.2.0
httpx==0.27.2
pytest==8.3.3
pytest-mock==3.14.0
```

- [ ] **Step 7: Write `backend/.env.example`**

```
RUNPOD_VIDEO_KEY=changeme
RUNPOD_IMAGE_KEY=changeme
RUNPOD_VIDEO_ENDPOINT=https://api.runpod.ai/v2/CHANGEME/runsync
RUNPOD_IMAGE_ENDPOINT=https://api.runpod.ai/v2/CHANGEME/runsync
R2_WRITE_KEY=changeme
R2_WRITE_SECRET=changeme
R2_READ_KEY=changeme
R2_READ_SECRET=changeme
R2_BUCKET=changeme
R2_ENDPOINT=https://CHANGEME.r2.cloudflarestorage.com
POSTGRES_DSN=postgresql://user:pass@localhost:5432/lantaw
REDIS_URL=redis://localhost:6379/0
BACKEND_API_KEY_HASH=changeme
WEBHOOK_URL=https://hooks.example.com/changeme
MODERATION_API_KEY=
```

- [ ] **Step 8: Write root `.gitignore`**

```
.env
__pycache__/
*.pyc
.venv/
venv/
*.egg-info/
.pytest_cache/
```

- [ ] **Step 9: Commit**

```bash
git add backend/app/__init__.py backend/app/config.py backend/app/logging_conf.py backend/requirements.txt backend/.env.example backend/tests/test_logging_conf.py .gitignore
git commit -m "feat: scaffold backend config and secret-redacting logging"
```

---

## Task 2: API Key Security (hash + timing-safe verify)

**Files:**
- Create: `backend/app/security.py`
- Test: `backend/tests/test_security.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `security.hash_api_key(raw: str) -> str`, `security.verify_api_key(provided: str, stored_hash: str) -> bool`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_security.py
from app.security import hash_api_key, verify_api_key


def test_hash_then_verify_succeeds():
    raw = "test-api-key-12345"
    hashed = hash_api_key(raw)
    assert verify_api_key(raw, hashed) is True


def test_verify_rejects_wrong_key():
    hashed = hash_api_key("correct-key")
    assert verify_api_key("wrong-key", hashed) is False


def test_hash_is_not_plaintext():
    raw = "test-api-key-12345"
    hashed = hash_api_key(raw)
    assert raw not in hashed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_security.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/security.py`**

```python
# backend/app/security.py
import hmac
import bcrypt


def hash_api_key(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_api_key(provided: str, stored_hash: str) -> bool:
    try:
        computed_ok = bcrypt.checkpw(provided.encode("utf-8"), stored_hash.encode("utf-8"))
    except ValueError:
        return False
    # bcrypt.checkpw is already constant-time for the hash comparison;
    # hmac.compare_digest guards the boolean-to-string path some callers use.
    return hmac.compare_digest(str(computed_ok), "True") if computed_ok else False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_security.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/security.py backend/tests/test_security.py
git commit -m "feat: add timing-safe API key hashing and verification"
```

---

## Task 3: Pydantic Request/Response Schemas (strict, no extra fields, no URL fields)

**Files:**
- Create: `backend/app/schemas.py`
- Test: `backend/tests/test_schemas.py`

**Interfaces:**
- Produces: `schemas.GenerateImageRequest(prompt: str)`, `schemas.GenerateVideoRequest(prompt: str, target_duration: float)`, `schemas.JobResponse(id, type, status, result_url, created_at)`, `schemas.ProjectResponse(id, status, clip_count, clips_complete, final_result_url)`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_schemas.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_schemas.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/schemas.py`**

```python
# backend/app/schemas.py
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class GenerateImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=2000)


class GenerateVideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=2000)
    target_duration: float = Field(gt=0, le=3600)


class JobResponse(BaseModel):
    id: str
    type: str
    status: str
    result_url: str | None = None
    created_at: datetime


class ProjectResponse(BaseModel):
    id: str
    status: str
    clip_count: int
    clips_complete: int
    final_result_url: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_schemas.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas.py backend/tests/test_schemas.py
git commit -m "feat: add strict request/response schemas rejecting extra fields"
```

---

## Task 4: Moderation Provider

**Files:**
- Create: `backend/app/moderation.py`
- Test: `backend/tests/test_moderation.py`

**Interfaces:**
- Consumes: `config.Settings.moderation_api_key`
- Produces: `moderation.ModerationResult(allowed: bool, reason: str | None)`, `moderation.KeywordModerationProvider.check(prompt: str) -> ModerationResult`, `moderation.ModerationProvider` (ABC with `.check`)

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_moderation.py
from app.moderation import KeywordModerationProvider, ModerationResult


def test_allows_benign_prompt():
    provider = KeywordModerationProvider(blocklist=["bomb", "cp"])
    result = provider.check("a cat playing piano in a sunny garden")
    assert isinstance(result, ModerationResult)
    assert result.allowed is True
    assert result.reason is None


def test_blocks_flagged_keyword():
    provider = KeywordModerationProvider(blocklist=["bomb", "cp"])
    result = provider.check("how to build a bomb")
    assert result.allowed is False
    assert "bomb" in result.reason


def test_blocklist_match_is_case_insensitive():
    provider = KeywordModerationProvider(blocklist=["bomb"])
    result = provider.check("How To Build A BOMB")
    assert result.allowed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_moderation.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/moderation.py`**

```python
# backend/app/moderation.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

DEFAULT_BLOCKLIST = [
    "child sexual", "csam", "bomb making", "how to build a bomb",
    "bioweapon", "chemical weapon synthesis",
]


@dataclass
class ModerationResult:
    allowed: bool
    reason: str | None = None


class ModerationProvider(ABC):
    @abstractmethod
    def check(self, prompt: str) -> ModerationResult:
        raise NotImplementedError


class KeywordModerationProvider(ModerationProvider):
    def __init__(self, blocklist: list[str] | None = None):
        self._blocklist = [b.lower() for b in (blocklist or DEFAULT_BLOCKLIST)]

    def check(self, prompt: str) -> ModerationResult:
        lowered = prompt.lower()
        for term in self._blocklist:
            if term in lowered:
                return ModerationResult(allowed=False, reason=f"blocked term: {term}")
        return ModerationResult(allowed=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_moderation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/moderation.py backend/tests/test_moderation.py
git commit -m "feat: add keyword-based moderation provider"
```

---

## Task 5: R2 Storage Client (scoped read/write keys, signed URLs)

**Files:**
- Create: `backend/app/r2.py`
- Test: `backend/tests/test_r2.py`

**Interfaces:**
- Consumes: `config.Settings` (r2_write_key, r2_write_secret, r2_read_key, r2_read_secret, r2_bucket, r2_endpoint)
- Produces: `r2.R2Client.__init__(settings: Settings)`, `r2.R2Client.upload(key: str, data: bytes, content_type: str) -> str`, `r2.R2Client.signed_url(key: str, expires_in: int = 3600) -> str`

- [ ] **Step 1: Write failing tests (mocking boto3 clients)**

```python
# backend/tests/test_r2.py
from unittest.mock import MagicMock, patch
from app.r2 import R2Client


def _fake_settings():
    return MagicMock(
        r2_write_key="wkey", r2_write_secret="wsecret",
        r2_read_key="rkey", r2_read_secret="rsecret",
        r2_bucket="test-bucket", r2_endpoint="https://example.r2.cloudflarestorage.com",
    )


@patch("app.r2.boto3.client")
def test_upload_uses_write_client_and_returns_key(mock_boto_client):
    write_client = MagicMock()
    mock_boto_client.return_value = write_client
    r2 = R2Client(_fake_settings())
    result_key = r2.upload("clips/abc.mp4", b"fake-bytes", "video/mp4")
    assert result_key == "clips/abc.mp4"
    write_client.put_object.assert_called_once()
    _, kwargs = write_client.put_object.call_args
    assert kwargs["Bucket"] == "test-bucket"
    assert kwargs["Key"] == "clips/abc.mp4"
    assert kwargs["Body"] == b"fake-bytes"
    assert kwargs["ContentType"] == "video/mp4"


@patch("app.r2.boto3.client")
def test_signed_url_uses_read_client(mock_boto_client):
    read_client = MagicMock()
    read_client.generate_presigned_url.return_value = "https://signed.example.com/x"
    mock_boto_client.return_value = read_client
    r2 = R2Client(_fake_settings())
    url = r2.signed_url("clips/abc.mp4", expires_in=120)
    assert url == "https://signed.example.com/x"
    read_client.generate_presigned_url.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_r2.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/r2.py`**

```python
# backend/app/r2.py
import boto3


class R2Client:
    def __init__(self, settings):
        self._bucket = settings.r2_bucket
        self._write_client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_write_key,
            aws_secret_access_key=settings.r2_write_secret,
        )
        self._read_client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_read_key,
            aws_secret_access_key=settings.r2_read_secret,
        )

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        self._write_client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type,
        )
        return key

    def signed_url(self, key: str, expires_in: int = 3600) -> str:
        return self._read_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_r2.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/r2.py backend/tests/test_r2.py
git commit -m "feat: add R2 client with scoped write/read credentials"
```

---

## Task 6: RunPod Dispatch Client

**Files:**
- Create: `backend/app/runpod_client.py`
- Test: `backend/tests/test_runpod_client.py`

**Interfaces:**
- Consumes: `config.Settings` (runpod_video_key, runpod_image_key, runpod_video_endpoint, runpod_image_endpoint)
- Produces: `runpod_client.RunpodClient.dispatch_video(prompt: str, duration: float) -> dict`, `runpod_client.RunpodClient.dispatch_image(prompt: str) -> dict`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_runpod_client.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_runpod_client.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/runpod_client.py`**

```python
# backend/app/runpod_client.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_runpod_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/runpod_client.py backend/tests/test_runpod_client.py
git commit -m "feat: add RunPod dispatch client for video and image workers"
```

---

## Task 7: TTS Stub Interface

**Files:**
- Create: `backend/app/tts.py`
- Test: `backend/tests/test_tts.py`

**Interfaces:**
- Produces: `tts.TTSProvider` (ABC), `tts.NullTTSProvider.generate(text: str, target_duration: float) -> str`

- [ ] **Step 1: Write failing test**

```python
# backend/tests/test_tts.py
import os
import wave
from app.tts import NullTTSProvider


def test_null_provider_returns_silent_wav_of_target_duration(tmp_path):
    provider = NullTTSProvider(output_dir=str(tmp_path))
    path = provider.generate(text="unused", target_duration=2.0)
    assert os.path.exists(path)
    with wave.open(path, "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        duration = frames / float(rate)
    assert abs(duration - 2.0) < 0.05
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_tts.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/tts.py`**

```python
# backend/app/tts.py
import os
import uuid
import wave
from abc import ABC, abstractmethod


class TTSProvider(ABC):
    @abstractmethod
    def generate(self, text: str, target_duration: float) -> str:
        raise NotImplementedError


class NullTTSProvider(TTSProvider):
    def __init__(self, output_dir: str = "/tmp"):
        self._output_dir = output_dir

    def generate(self, text: str, target_duration: float) -> str:
        sample_rate = 44100
        n_frames = int(sample_rate * target_duration)
        path = os.path.join(self._output_dir, f"silence_{uuid.uuid4().hex}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(b"\x00\x00" * n_frames)
        return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_tts.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/tts.py backend/tests/test_tts.py
git commit -m "feat: add TTS provider interface with silent-audio stub"
```

---

## Task 8: Database Models + Session

**Files:**
- Create: `backend/app/db.py`, `backend/app/models.py`
- Test: `backend/tests/test_models.py`

**Interfaces:**
- Produces: `db.get_engine(dsn: str)`, `db.session_factory(engine)`, `models.Base`, `models.Job`, `models.VideoProject`, `models.ProjectClip`

- [ ] **Step 1: Write failing test (uses in-memory SQLite for speed, Postgres-compatible schema via SQLAlchemy)**

```python
# backend/tests/test_models.py
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Job, VideoProject, ProjectClip


def test_create_job_and_project_with_clips():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    project_id = str(uuid.uuid4())
    project = VideoProject(
        id=project_id, api_key_id="key1", target_duration=20.0,
        clip_count=2, status="pending",
    )
    session.add(project)

    job1 = Job(id=str(uuid.uuid4()), api_key_id="key1", type="clip",
               prompt="scene one", duration=10.0, status="pending", retry_count=0)
    job2 = Job(id=str(uuid.uuid4()), api_key_id="key1", type="clip",
               prompt="scene two", duration=10.0, status="pending", retry_count=0)
    session.add_all([job1, job2])
    session.flush()

    session.add(ProjectClip(project_id=project_id, sequence_index=0, job_id=job1.id))
    session.add(ProjectClip(project_id=project_id, sequence_index=1, job_id=job2.id))
    session.commit()

    fetched = session.query(VideoProject).filter_by(id=project_id).one()
    assert fetched.clip_count == 2
    clips = session.query(ProjectClip).filter_by(project_id=project_id).order_by(ProjectClip.sequence_index).all()
    assert [c.job_id for c in clips] == [job1.id, job2.id]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_models.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/models.py`**

```python
# backend/app/models.py
from datetime import datetime, timezone
from sqlalchemy import String, Float, Integer, DateTime, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    api_key_id: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(16))  # "clip" | "image"
    prompt: Mapped[str] = mapped_column(String(2000))
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    result_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class VideoProject(Base):
    __tablename__ = "video_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    api_key_id: Mapped[str] = mapped_column(String(64), index=True)
    target_duration: Mapped[float] = mapped_column(Float)
    clip_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    final_result_key: Mapped[str | None] = mapped_column(String(512), nullable=True)


class ProjectClip(Base):
    __tablename__ = "project_clips"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("video_projects.id"), primary_key=True)
    sequence_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"))
```

- [ ] **Step 4: Write `backend/app/db.py`**

```python
# backend/app/db.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def get_engine(dsn: str):
    return create_engine(dsn, pool_pre_ping=True)


def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/db.py backend/app/models.py backend/tests/test_models.py
git commit -m "feat: add SQLAlchemy models for jobs, projects, and clips"
```

---

## Task 9: Rate Limiting Middleware (Redis sliding window)

**Files:**
- Create: `backend/app/middleware/__init__.py`, `backend/app/middleware/rate_limit.py`
- Test: `backend/tests/test_rate_limit.py`

**Interfaces:**
- Consumes: a `redis.Redis`-like client (duck-typed for testability)
- Produces: `rate_limit.SlidingWindowLimiter.__init__(redis_client, max_requests: int, window_seconds: int)`, `rate_limit.SlidingWindowLimiter.is_allowed(key: str) -> bool`

- [ ] **Step 1: Write failing tests (using fakeredis-style in-memory stand-in via real `redis` if available, else a minimal fake)**

```python
# backend/tests/test_rate_limit.py
import time
from app.middleware.rate_limit import SlidingWindowLimiter


class FakeRedis:
    def __init__(self):
        self._store = {}

    def zremrangebyscore(self, key, min_score, max_score):
        self._store.setdefault(key, [])
        self._store[key] = [t for t in self._store[key] if not (min_score <= t <= max_score)]

    def zcard(self, key):
        return len(self._store.get(key, []))

    def zadd(self, key, mapping):
        self._store.setdefault(key, [])
        self._store[key].extend(mapping.keys())

    def expire(self, key, seconds):
        pass


def test_allows_requests_under_limit():
    limiter = SlidingWindowLimiter(FakeRedis(), max_requests=3, window_seconds=60)
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is True


def test_blocks_requests_over_limit():
    limiter = SlidingWindowLimiter(FakeRedis(), max_requests=2, window_seconds=60)
    assert limiter.is_allowed("client2") is True
    assert limiter.is_allowed("client2") is True
    assert limiter.is_allowed("client2") is False


def test_limits_are_per_key():
    redis = FakeRedis()
    limiter = SlidingWindowLimiter(redis, max_requests=1, window_seconds=60)
    assert limiter.is_allowed("clientA") is True
    assert limiter.is_allowed("clientB") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_rate_limit.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/middleware/__init__.py` (empty) and `backend/app/middleware/rate_limit.py`**

```python
# backend/app/middleware/rate_limit.py
import time


class SlidingWindowLimiter:
    def __init__(self, redis_client, max_requests: int, window_seconds: int):
        self._redis = redis_client
        self._max_requests = max_requests
        self._window_seconds = window_seconds

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window_start = now - self._window_seconds
        redis_key = f"ratelimit:{key}"
        self._redis.zremrangebyscore(redis_key, 0, window_start)
        current_count = self._redis.zcard(redis_key)
        if current_count >= self._max_requests:
            return False
        self._redis.zadd(redis_key, {f"{now}:{id(object())}": now})
        self._redis.expire(redis_key, self._window_seconds)
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_rate_limit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/middleware/__init__.py backend/app/middleware/rate_limit.py backend/tests/test_rate_limit.py
git commit -m "feat: add Redis sliding-window rate limiter"
```

---

## Task 10: Auth Dependency (FastAPI)

**Files:**
- Create: `backend/app/middleware/auth.py`
- Test: `backend/tests/test_auth.py`

**Interfaces:**
- Consumes: `security.verify_api_key`, `config.Settings.backend_api_key_hash`
- Produces: `auth.require_api_key(authorization: str | None = Header(None)) -> str` (FastAPI dependency; returns an `api_key_id` string derived from the key, raises `HTTPException(401)` on missing/invalid)

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_auth.py
import pytest
from fastapi import HTTPException
from app.middleware.auth import make_auth_dependency
from app.security import hash_api_key


def test_valid_bearer_key_passes():
    stored_hash = hash_api_key("real-key")
    dependency = make_auth_dependency(stored_hash=stored_hash)
    result = dependency(authorization="Bearer real-key")
    assert result == "primary"


def test_missing_header_rejected():
    stored_hash = hash_api_key("real-key")
    dependency = make_auth_dependency(stored_hash=stored_hash)
    with pytest.raises(HTTPException) as exc_info:
        dependency(authorization=None)
    assert exc_info.value.status_code == 401


def test_wrong_key_rejected():
    stored_hash = hash_api_key("real-key")
    dependency = make_auth_dependency(stored_hash=stored_hash)
    with pytest.raises(HTTPException) as exc_info:
        dependency(authorization="Bearer wrong-key")
    assert exc_info.value.status_code == 401


def test_malformed_header_rejected():
    stored_hash = hash_api_key("real-key")
    dependency = make_auth_dependency(stored_hash=stored_hash)
    with pytest.raises(HTTPException) as exc_info:
        dependency(authorization="NotBearer real-key")
    assert exc_info.value.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_auth.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/middleware/auth.py`**

```python
# backend/app/middleware/auth.py
from fastapi import Header, HTTPException
from app.security import verify_api_key


def make_auth_dependency(stored_hash: str):
    def require_api_key(authorization: str | None = Header(None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing or malformed Authorization header")
        provided = authorization.removeprefix("Bearer ").strip()
        if not provided or not verify_api_key(provided, stored_hash):
            raise HTTPException(status_code=401, detail="invalid API key")
        return "primary"

    return require_api_key
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/middleware/auth.py backend/tests/test_auth.py
git commit -m "feat: add Bearer API key auth dependency"
```

---

## Task 11: Stitcher (ffmpeg concat + audio mux)

**Files:**
- Create: `backend/app/stitcher.py`
- Test: `backend/tests/test_stitcher.py`

**Interfaces:**
- Consumes: `tts.TTSProvider` (for generating the stub audio track in the calling code, not inside stitcher itself)
- Produces: `stitcher.stitch_project(clip_paths: list[str], audio_path: str, output_path: str) -> None`

Note: real ffmpeg execution is mocked in tests — this task validates command construction and file-list handling, not actual video encoding (that's verified manually in Task 17's integration check).

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_stitcher.py
import os
from unittest.mock import patch, MagicMock
from app.stitcher import stitch_project, build_concat_file


def test_build_concat_file_lists_clips_in_order(tmp_path):
    clip_paths = [str(tmp_path / "clip0.mp4"), str(tmp_path / "clip1.mp4")]
    for p in clip_paths:
        open(p, "wb").close()
    concat_path = build_concat_file(clip_paths, str(tmp_path / "concat.txt"))
    content = open(concat_path).read()
    lines = [l for l in content.splitlines() if l.strip()]
    assert lines[0] == f"file '{clip_paths[0]}'"
    assert lines[1] == f"file '{clip_paths[1]}'"


@patch("app.stitcher.subprocess.run")
def test_stitch_project_invokes_ffmpeg_with_concat_and_audio(mock_run, tmp_path):
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    clip_paths = [str(tmp_path / "clip0.mp4")]
    open(clip_paths[0], "wb").close()
    audio_path = str(tmp_path / "silence.wav")
    open(audio_path, "wb").close()
    output_path = str(tmp_path / "final.mp4")

    stitch_project(clip_paths, audio_path, output_path)

    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert "ffmpeg" in call_args
    assert audio_path in call_args
    assert output_path in call_args


@patch("app.stitcher.subprocess.run")
def test_stitch_project_raises_on_ffmpeg_failure(mock_run, tmp_path):
    mock_run.return_value = MagicMock(returncode=1, stderr=b"ffmpeg exploded")
    clip_paths = [str(tmp_path / "clip0.mp4")]
    open(clip_paths[0], "wb").close()
    audio_path = str(tmp_path / "silence.wav")
    open(audio_path, "wb").close()
    try:
        stitch_project(clip_paths, audio_path, str(tmp_path / "final.mp4"))
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "ffmpeg" in str(e).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_stitcher.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/stitcher.py`**

```python
# backend/app/stitcher.py
import subprocess


def build_concat_file(clip_paths: list[str], concat_file_path: str) -> str:
    with open(concat_file_path, "w") as f:
        for path in clip_paths:
            f.write(f"file '{path}'\n")
    return concat_file_path


def stitch_project(clip_paths: list[str], audio_path: str, output_path: str) -> None:
    concat_file_path = output_path + ".concat.txt"
    build_concat_file(clip_paths, concat_file_path)

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_file_path,
        "-i", audio_path,
        "-c:v", "copy", "-c:a", "aac",
        "-shortest",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg stitch failed: {result.stderr.decode(errors='replace')}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_stitcher.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/stitcher.py backend/tests/test_stitcher.py
git commit -m "feat: add ffmpeg-based clip stitcher with audio mux"
```

---

## Task 12: RQ Queue Jobs (dispatch clip/image, stitch)

**Files:**
- Create: `backend/app/queue.py`
- Test: `backend/tests/test_queue.py`

**Interfaces:**
- Consumes: `runpod_client.RunpodClient`, `r2.R2Client`, `moderation.ModerationProvider`, `models.Job`, `db.session_factory`
- Produces: `queue.process_clip_job(job_id: str, session_factory, runpod_client, r2_client) -> None`, `queue.process_image_job(job_id: str, session_factory, runpod_client, r2_client) -> None`, `queue.MAX_RETRIES = 3`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_queue.py
import uuid
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Job
from app.queue import process_clip_job, process_image_job


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_process_clip_job_marks_complete_on_success():
    Session = _make_session_factory()
    session = Session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, api_key_id="k", type="clip", prompt="p", duration=10.0, status="pending", retry_count=0))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.return_value = {"output": {"key": "clips/x.mp4", "bytes_b64": "AA=="}}
    r2_client = MagicMock()
    r2_client.upload.return_value = "clips/x.mp4"

    process_clip_job(job_id, Session, runpod_client, r2_client)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"
    assert updated.result_key == "clips/x.mp4"


def test_process_clip_job_marks_failed_after_max_retries():
    Session = _make_session_factory()
    session = Session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, api_key_id="k", type="clip", prompt="p", duration=10.0, status="pending", retry_count=3))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.side_effect = RuntimeError("upstream down")
    r2_client = MagicMock()

    process_clip_job(job_id, Session, runpod_client, r2_client)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "failed"


def test_process_image_job_marks_complete_on_success():
    Session = _make_session_factory()
    session = Session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, api_key_id="k", type="image", prompt="p", duration=None, status="pending", retry_count=0))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_image.return_value = {"output": {"key": "images/x.png", "bytes_b64": "AA=="}}
    r2_client = MagicMock()
    r2_client.upload.return_value = "images/x.png"

    process_image_job(job_id, Session, runpod_client, r2_client)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"
    assert updated.result_key == "images/x.png"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_queue.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/queue.py`**

```python
# backend/app/queue.py
import base64

MAX_RETRIES = 3


def _mark_failed_or_retry(session, job) -> bool:
    """Returns True if job should be retried (caller re-raises/re-enqueues), False if exhausted."""
    if job.retry_count >= MAX_RETRIES:
        job.status = "failed"
        session.commit()
        return False
    job.retry_count += 1
    session.commit()
    return True


def process_clip_job(job_id: str, session_factory, runpod_client, r2_client) -> None:
    session = session_factory()
    job = session.query(session.get_bind() and _job_model()).get(job_id) if False else None
    from app.models import Job
    job = session.query(Job).filter_by(id=job_id).one()

    try:
        result = runpod_client.dispatch_video(prompt=job.prompt, duration=job.duration)
        raw_bytes = base64.b64decode(result["output"]["bytes_b64"])
        key = result["output"]["key"]
        r2_client.upload(key, raw_bytes, "video/mp4")
        job.status = "complete"
        job.result_key = key
        session.commit()
    except Exception:
        _mark_failed_or_retry(session, job)


def process_image_job(job_id: str, session_factory, runpod_client, r2_client) -> None:
    from app.models import Job
    session = session_factory()
    job = session.query(Job).filter_by(id=job_id).one()

    try:
        result = runpod_client.dispatch_image(prompt=job.prompt)
        raw_bytes = base64.b64decode(result["output"]["bytes_b64"])
        key = result["output"]["key"]
        r2_client.upload(key, raw_bytes, "image/png")
        job.status = "complete"
        job.result_key = key
        session.commit()
    except Exception:
        _mark_failed_or_retry(session, job)
```

- [ ] **Step 4: Run test to verify it passes, then clean up dead code**

Run: `cd backend && python -m pytest tests/test_queue.py -v`
Expected: PASS

Remove the unreachable `if False` line in `process_clip_job` (leftover from drafting) — replace:

```python
    session = session_factory()
    job = session.query(session.get_bind() and _job_model()).get(job_id) if False else None
    from app.models import Job
    job = session.query(Job).filter_by(id=job_id).one()
```

with:

```python
    from app.models import Job
    session = session_factory()
    job = session.query(Job).filter_by(id=job_id).one()
```

Re-run: `cd backend && python -m pytest tests/test_queue.py -v` — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue.py backend/tests/test_queue.py
git commit -m "feat: add RQ job functions for clip/image dispatch with retry"
```

---

## Task 13: Routes — POST /generate-image, POST /generate-video

**Files:**
- Create: `backend/app/routes/__init__.py`, `backend/app/routes/generate_image.py`, `backend/app/routes/generate_video.py`
- Test: `backend/tests/test_routes_image.py`, `backend/tests/test_routes_video.py`

**Interfaces:**
- Consumes: `schemas.GenerateImageRequest`, `schemas.GenerateVideoRequest`, `moderation.ModerationProvider`, `models.Job`, `models.VideoProject`, `models.ProjectClip`, `middleware.auth.require_api_key`, `middleware.rate_limit.SlidingWindowLimiter`
- Produces: FastAPI `APIRouter` instances `generate_image.router`, `generate_video.router`

- [ ] **Step 1: Write failing tests using FastAPI `TestClient` with dependency overrides**

```python
# backend/tests/test_routes_image.py
import uuid
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base
from app.routes.generate_image import router, get_db_session, get_moderation, get_queue_enqueue


def _build_app():
    app = FastAPI()
    app.include_router(router)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    class AllowAllModeration:
        def check(self, prompt):
            from app.moderation import ModerationResult
            return ModerationResult(allowed=True)

    enqueued = []

    def fake_enqueue(job_id):
        enqueued.append(job_id)

    app.dependency_overrides[get_db_session] = lambda: Session()
    app.dependency_overrides[get_moderation] = lambda: AllowAllModeration()
    app.dependency_overrides[get_queue_enqueue] = lambda: fake_enqueue
    return app, enqueued


def test_generate_image_creates_job_and_enqueues():
    app, enqueued = _build_app()
    client = TestClient(app)
    response = client.post("/generate-image", json={"prompt": "a red fox"}, headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 202
    body = response.json()
    assert "id" in body
    assert body["status"] == "pending"
    assert len(enqueued) == 1


def test_generate_image_rejects_blocked_prompt():
    app, enqueued = _build_app()

    class BlockAllModeration:
        def check(self, prompt):
            from app.moderation import ModerationResult
            return ModerationResult(allowed=False, reason="blocked term: x")

    app.dependency_overrides[get_moderation] = lambda: BlockAllModeration()
    client = TestClient(app)
    response = client.post("/generate-image", json={"prompt": "bad prompt"}, headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 422
    assert len(enqueued) == 0


def test_generate_image_rejects_extra_fields():
    app, _ = _build_app()
    client = TestClient(app)
    response = client.post(
        "/generate-image",
        json={"prompt": "a fox", "image_url": "http://evil.example.com"},
        headers={"X-Api-Key-Id": "primary"},
    )
    assert response.status_code == 422
```

```python
# backend/tests/test_routes_video.py
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, VideoProject, ProjectClip
from app.routes.generate_video import router, get_db_session, get_moderation, get_queue_enqueue


def _build_app():
    app = FastAPI()
    app.include_router(router)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    class AllowAllModeration:
        def check(self, prompt):
            from app.moderation import ModerationResult
            return ModerationResult(allowed=True)

    enqueued = []

    def fake_enqueue(job_id):
        enqueued.append(job_id)

    session_holder = {"session": Session()}
    app.dependency_overrides[get_db_session] = lambda: session_holder["session"]
    app.dependency_overrides[get_moderation] = lambda: AllowAllModeration()
    app.dependency_overrides[get_queue_enqueue] = lambda: fake_enqueue
    return app, enqueued, session_holder


def test_generate_video_splits_into_clip_jobs():
    app, enqueued, session_holder = _build_app()
    client = TestClient(app)
    response = client.post(
        "/generate-video", json={"prompt": "a journey through a forest", "target_duration": 25},
        headers={"X-Api-Key-Id": "primary"},
    )
    assert response.status_code == 202
    body = response.json()
    project_id = body["id"]
    assert body["clip_count"] == 3  # ceil(25/10)
    assert len(enqueued) == 3

    session = session_holder["session"]
    project = session.query(VideoProject).filter_by(id=project_id).one()
    assert project.status == "pending"
    clips = session.query(ProjectClip).filter_by(project_id=project_id).order_by(ProjectClip.sequence_index).all()
    assert [c.sequence_index for c in clips] == [0, 1, 2]


def test_generate_video_rejects_out_of_range_duration():
    app, enqueued, _ = _build_app()
    client = TestClient(app)
    response = client.post(
        "/generate-video", json={"prompt": "x", "target_duration": -5},
        headers={"X-Api-Key-Id": "primary"},
    )
    assert response.status_code == 422
    assert len(enqueued) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_routes_image.py tests/test_routes_video.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/routes/__init__.py` (empty), `backend/app/routes/generate_image.py`**

```python
# backend/app/routes/generate_image.py
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas import GenerateImageRequest, JobResponse
from app.models import Job

router = APIRouter()


def get_db_session():
    raise NotImplementedError("override in app wiring")


def get_moderation():
    raise NotImplementedError("override in app wiring")


def get_queue_enqueue():
    raise NotImplementedError("override in app wiring")


@router.post("/generate-image", status_code=status.HTTP_202_ACCEPTED, response_model=JobResponse)
def generate_image(
    body: GenerateImageRequest,
    session=Depends(get_db_session),
    moderation=Depends(get_moderation),
    enqueue=Depends(get_queue_enqueue),
):
    result = moderation.check(body.prompt)
    if not result.allowed:
        raise HTTPException(status_code=422, detail="prompt rejected by moderation")

    job_id = str(uuid.uuid4())
    job = Job(id=job_id, api_key_id="primary", type="image", prompt=body.prompt,
              duration=None, status="pending", retry_count=0)
    session.add(job)
    session.commit()
    enqueue(job_id)

    return JobResponse(id=job_id, type="image", status="pending", result_url=None, created_at=job.created_at)
```

- [ ] **Step 4: Write `backend/app/routes/generate_video.py`**

```python
# backend/app/routes/generate_video.py
import math
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas import GenerateVideoRequest, ProjectResponse
from app.models import Job, VideoProject, ProjectClip

router = APIRouter()

PRO_TIER_CLIP_SECONDS = 10


def get_db_session():
    raise NotImplementedError("override in app wiring")


def get_moderation():
    raise NotImplementedError("override in app wiring")


def get_queue_enqueue():
    raise NotImplementedError("override in app wiring")


@router.post("/generate-video", status_code=status.HTTP_202_ACCEPTED, response_model=ProjectResponse)
def generate_video(
    body: GenerateVideoRequest,
    session=Depends(get_db_session),
    moderation=Depends(get_moderation),
    enqueue=Depends(get_queue_enqueue),
):
    result = moderation.check(body.prompt)
    if not result.allowed:
        raise HTTPException(status_code=422, detail="prompt rejected by moderation")

    clip_count = math.ceil(body.target_duration / PRO_TIER_CLIP_SECONDS)
    project_id = str(uuid.uuid4())
    project = VideoProject(id=project_id, api_key_id="primary", target_duration=body.target_duration,
                            clip_count=clip_count, status="pending")
    session.add(project)

    for i in range(clip_count):
        job_id = str(uuid.uuid4())
        job = Job(id=job_id, api_key_id="primary", type="clip", prompt=body.prompt,
                  duration=float(PRO_TIER_CLIP_SECONDS), status="pending", retry_count=0)
        session.add(job)
        session.flush()
        session.add(ProjectClip(project_id=project_id, sequence_index=i, job_id=job_id))

    session.commit()

    for clip in session.query(ProjectClip).filter_by(project_id=project_id).all():
        enqueue(clip.job_id)

    return ProjectResponse(id=project_id, status="pending", clip_count=clip_count,
                            clips_complete=0, final_result_url=None)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_routes_image.py tests/test_routes_video.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/__init__.py backend/app/routes/generate_image.py backend/app/routes/generate_video.py backend/tests/test_routes_image.py backend/tests/test_routes_video.py
git commit -m "feat: add generate-image and generate-video routes with moderation gate"
```

---

## Task 14: Routes — GET /jobs/{id}, GET /projects/{id} (ownership-checked)

**Files:**
- Create: `backend/app/routes/jobs.py`
- Test: `backend/tests/test_routes_jobs.py`

**Interfaces:**
- Consumes: `models.Job`, `models.VideoProject`, `models.ProjectClip`, `r2.R2Client.signed_url`
- Produces: `jobs.router` with `GET /jobs/{id}`, `GET /projects/{id}`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_routes_jobs.py
import uuid
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Job, VideoProject, ProjectClip
from app.routes.jobs import router, get_db_session, get_r2_client


def _build_app_with_data():
    app = FastAPI()
    app.include_router(router)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()

    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, api_key_id="primary", type="image", prompt="p",
                     duration=None, status="complete", retry_count=0, result_key="images/x.png"))

    other_job_id = str(uuid.uuid4())
    session.add(Job(id=other_job_id, api_key_id="someone-else", type="image", prompt="p",
                     duration=None, status="complete", retry_count=0, result_key="images/y.png"))

    project_id = str(uuid.uuid4())
    session.add(VideoProject(id=project_id, api_key_id="primary", target_duration=10.0,
                              clip_count=1, status="complete", final_result_key="videos/final.mp4"))
    clip_job_id = str(uuid.uuid4())
    session.add(Job(id=clip_job_id, api_key_id="primary", type="clip", prompt="p",
                     duration=10.0, status="complete", retry_count=0, result_key="clips/c0.mp4"))
    session.flush()
    session.add(ProjectClip(project_id=project_id, sequence_index=0, job_id=clip_job_id))
    session.commit()

    class FakeR2:
        def signed_url(self, key, expires_in=3600):
            return f"https://signed.example.com/{key}"

    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_r2_client] = lambda: FakeR2()
    return app, job_id, other_job_id, project_id


def test_get_job_returns_signed_url_for_owner():
    app, job_id, _, _ = _build_app_with_data()
    client = TestClient(app)
    response = client.get(f"/jobs/{job_id}", headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete"
    assert body["result_url"] == "https://signed.example.com/images/x.png"


def test_get_job_rejects_non_owner():
    app, _, other_job_id, _ = _build_app_with_data()
    client = TestClient(app)
    response = client.get(f"/jobs/{other_job_id}", headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 404


def test_get_job_unknown_id_returns_404():
    app, _, _, _ = _build_app_with_data()
    client = TestClient(app)
    response = client.get(f"/jobs/{uuid.uuid4()}", headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 404


def test_get_project_returns_progress_and_final_url():
    app, _, _, project_id = _build_app_with_data()
    client = TestClient(app)
    response = client.get(f"/projects/{project_id}", headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 200
    body = response.json()
    assert body["clip_count"] == 1
    assert body["clips_complete"] == 1
    assert body["final_result_url"] == "https://signed.example.com/videos/final.mp4"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_routes_jobs.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/routes/jobs.py`**

```python
# backend/app/routes/jobs.py
from fastapi import APIRouter, Depends, HTTPException
from app.models import Job, VideoProject, ProjectClip
from app.schemas import JobResponse, ProjectResponse

router = APIRouter()

CURRENT_API_KEY_ID = "primary"  # single trusted client for now


def get_db_session():
    raise NotImplementedError("override in app wiring")


def get_r2_client():
    raise NotImplementedError("override in app wiring")


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, session=Depends(get_db_session), r2_client=Depends(get_r2_client)):
    job = session.query(Job).filter_by(id=job_id, api_key_id=CURRENT_API_KEY_ID).one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    result_url = r2_client.signed_url(job.result_key) if job.result_key else None
    return JobResponse(id=job.id, type=job.type, status=job.status, result_url=result_url, created_at=job.created_at)


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, session=Depends(get_db_session), r2_client=Depends(get_r2_client)):
    project = session.query(VideoProject).filter_by(id=project_id, api_key_id=CURRENT_API_KEY_ID).one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    clips = session.query(ProjectClip).filter_by(project_id=project_id).all()
    job_ids = [c.job_id for c in clips]
    complete_count = session.query(Job).filter(Job.id.in_(job_ids), Job.status == "complete").count() if job_ids else 0

    final_url = r2_client.signed_url(project.final_result_key) if project.final_result_key else None
    return ProjectResponse(id=project.id, status=project.status, clip_count=project.clip_count,
                            clips_complete=complete_count, final_result_url=final_url)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_routes_jobs.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/jobs.py backend/tests/test_routes_jobs.py
git commit -m "feat: add ownership-checked job and project status routes"
```

---

## Task 15: FastAPI App Wiring (main.py) + Generic Error Handler

**Files:**
- Create: `backend/app/main.py`
- Test: `backend/tests/test_main.py`

**Interfaces:**
- Consumes: everything from Tasks 1-14
- Produces: `main.create_app() -> FastAPI`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_main.py
import os
import uuid
from fastapi.testclient import TestClient


def _set_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNPOD_VIDEO_KEY", "vkey")
    monkeypatch.setenv("RUNPOD_IMAGE_KEY", "ikey")
    monkeypatch.setenv("RUNPOD_VIDEO_ENDPOINT", "https://api.runpod.ai/v2/vid/runsync")
    monkeypatch.setenv("RUNPOD_IMAGE_ENDPOINT", "https://api.runpod.ai/v2/img/runsync")
    monkeypatch.setenv("R2_WRITE_KEY", "wkey")
    monkeypatch.setenv("R2_WRITE_SECRET", "wsecret")
    monkeypatch.setenv("R2_READ_KEY", "rkey")
    monkeypatch.setenv("R2_READ_SECRET", "rsecret")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.setenv("R2_ENDPOINT", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setenv("POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    from app.security import hash_api_key
    monkeypatch.setenv("BACKEND_API_KEY_HASH", hash_api_key("test-key"))
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.com/x")


def test_unauthenticated_request_rejected(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    client = TestClient(app)
    response = client.post("/generate-image", json={"prompt": "a fox"})
    assert response.status_code == 401


def test_wrong_key_rejected(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    client = TestClient(app)
    response = client.post("/generate-image", json={"prompt": "a fox"},
                            headers={"Authorization": "Bearer wrong-key"})
    assert response.status_code == 401


def test_unhandled_exception_returns_generic_500_with_correlation_id(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()

    @app.get("/__boom")
    def boom():
        raise ValueError("internal secret detail: sk-abc123")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/__boom")
    assert response.status_code == 500
    body = response.json()
    assert "sk-abc123" not in response.text
    assert "correlation_id" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_main.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `backend/app/main.py`**

```python
# backend/app/main.py
import logging
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import redis as redis_lib

from app.config import get_settings
from app.logging_conf import configure_logging
from app.db import get_engine, session_factory
from app.models import Base
from app.moderation import KeywordModerationProvider
from app.r2 import R2Client
from app.runpod_client import RunpodClient
from app.middleware.auth import make_auth_dependency
from app.middleware.rate_limit import SlidingWindowLimiter
from app.routes import generate_image, generate_video, jobs
from app.queue import process_clip_job, process_image_job
from app.models import Job

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.all_secrets())

    app = FastAPI()

    engine = get_engine(settings.postgres_dsn)
    Base.metadata.create_all(engine)
    Session = session_factory(engine)

    moderation = KeywordModerationProvider()
    r2_client = R2Client(settings)
    runpod_client = RunpodClient(settings)
    redis_client = redis_lib.from_url(settings.redis_url)
    limiter = SlidingWindowLimiter(redis_client, max_requests=10, window_seconds=60)

    auth_dependency = make_auth_dependency(settings.backend_api_key_hash)

    def db_session_override():
        return Session()

    def moderation_override():
        return moderation

    def r2_override():
        return r2_client

    def make_enqueue(job_type: str):
        def enqueue(job_id: str):
            if job_type == "clip":
                process_clip_job(job_id, Session, runpod_client, r2_client)
            else:
                process_image_job(job_id, Session, runpod_client, r2_client)
        return enqueue

    app.include_router(generate_image.router, dependencies=[])
    app.include_router(generate_video.router, dependencies=[])
    app.include_router(jobs.router, dependencies=[])

    app.dependency_overrides[generate_image.get_db_session] = db_session_override
    app.dependency_overrides[generate_image.get_moderation] = moderation_override
    app.dependency_overrides[generate_image.get_queue_enqueue] = lambda: make_enqueue("image")

    app.dependency_overrides[generate_video.get_db_session] = db_session_override
    app.dependency_overrides[generate_video.get_moderation] = moderation_override
    app.dependency_overrides[generate_video.get_queue_enqueue] = lambda: make_enqueue("clip")

    app.dependency_overrides[jobs.get_db_session] = db_session_override
    app.dependency_overrides[jobs.get_r2_client] = r2_override

    @app.middleware("http")
    async def enforce_auth_and_rate_limit(request: Request, call_next):
        if request.url.path in ("/generate-image", "/generate-video") or request.url.path.startswith(("/jobs/", "/projects/")):
            try:
                auth_dependency(request.headers.get("authorization"))
            except Exception as exc:
                status_code = getattr(exc, "status_code", 401)
                detail = getattr(exc, "detail", "unauthorized")
                return JSONResponse(status_code=status_code, content={"detail": detail})

            if not limiter.is_allowed(request.headers.get("authorization", "unknown")):
                return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})

        return await call_next(request)

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        correlation_id = str(uuid.uuid4())
        logger.error("unhandled exception [correlation_id=%s]: %s", correlation_id, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error", "correlation_id": correlation_id},
        )

    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_main.py -v`
Expected: PASS

- [ ] **Step 5: Run full backend test suite**

Run: `cd backend && python -m pytest -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/main.py backend/tests/test_main.py
git commit -m "feat: wire FastAPI app with auth, rate limiting, and generic error handling"
```

---

## Task 16: Backend Dockerfile + Caddy TLS Sidecar + docker-compose

**Files:**
- Create: `backend/Dockerfile`, `Caddyfile`, `docker-compose.yml`

**Interfaces:**
- Consumes: `backend/requirements.txt`, `backend/app/main.py`
- Produces: running containerized stack (verified manually, no unit test — this is infra config)

- [ ] **Step 1: Write `backend/Dockerfile`**

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd -m appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Write `Caddyfile`**

```
{$BACKEND_DOMAIN} {
    reverse_proxy backend:8000
}
```

- [ ] **Step 3: Write `docker-compose.yml`**

```yaml
version: "3.9"

services:
  caddy:
    image: caddy:2-alpine
    ports:
      - "443:443"
      - "80:80"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
    environment:
      - BACKEND_DOMAIN=${BACKEND_DOMAIN}
    depends_on:
      - backend

  backend:
    build: ./backend
    env_file:
      - backend/.env
    depends_on:
      - postgres
      - redis
    expose:
      - "8000"

  rq_worker:
    build: ./backend
    command: rq worker --url ${REDIS_URL}
    env_file:
      - backend/.env
    depends_on:
      - postgres
      - redis

  postgres:
    image: postgres:16-alpine
    environment:
      - POSTGRES_USER=lantaw
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - POSTGRES_DB=lantaw
    volumes:
      - postgres_data:/var/lib/postgresql/data
    expose:
      - "5432"

  redis:
    image: redis:7-alpine
    expose:
      - "6379"

volumes:
  caddy_data:
  postgres_data:
```

- [ ] **Step 4: Verify compose file is syntactically valid**

Run: `docker compose config`
Expected: prints resolved config with no errors (env vars unresolved are fine at this stage — real values go in `backend/.env` and a root `.env` for `BACKEND_DOMAIN`/`POSTGRES_PASSWORD`, created manually before first deploy, never committed)

- [ ] **Step 5: Commit**

```bash
git add backend/Dockerfile Caddyfile docker-compose.yml
git commit -m "feat: add backend Dockerfile, Caddy TLS sidecar, and docker-compose stack"
```

---

## Task 17: Video Worker Handler (RunPod)

**Files:**
- Create: `worker-video/handler.py`, `worker-video/Dockerfile`, `worker-video/requirements.txt`
- Test: `worker-video/tests/test_handler.py`

**Interfaces:**
- Produces: `handler.validate_input(job_input: dict) -> tuple[str, float]` (returns validated prompt, duration; raises `ValueError` on bad input), `handler.select_model_variant(duration: float) -> str`, `handler.handler(job: dict) -> dict` (RunPod entrypoint)

- [ ] **Step 1: Write failing tests**

```python
# worker-video/tests/test_handler.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker-video && python -m pytest tests/test_handler.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `worker-video/handler.py`**

```python
# worker-video/handler.py
import base64
import uuid

RESOLUTION = "1080p"
FPS = 24
PRO_MAX_DURATION = 10
FAST_MAX_DURATION = 20

_MODEL = None  # loaded once at container start, see load_model()


def load_model():
    global _MODEL
    if _MODEL is None:
        # Real model load happens here (LTX-2.3 weights from the attached
        # Network Volume). Left as an integration point for the actual
        # LTX-2.3 runtime — this plan does not vendor model-loading code.
        _MODEL = {"loaded": True}
    return _MODEL


def validate_input(job_input: dict) -> tuple[str, float]:
    allowed_keys = {"prompt", "duration"}
    if set(job_input.keys()) - allowed_keys:
        raise ValueError(f"unexpected fields: {set(job_input.keys()) - allowed_keys}")
    if "prompt" not in job_input or not isinstance(job_input["prompt"], str) or not job_input["prompt"].strip():
        raise ValueError("prompt is required and must be a non-empty string")
    if "duration" not in job_input:
        raise ValueError("duration is required")
    duration = float(job_input["duration"])
    if not (0 < duration <= FAST_MAX_DURATION):
        raise ValueError(f"duration must be between 0 and {FAST_MAX_DURATION}")
    return job_input["prompt"], duration


def select_model_variant(duration: float) -> str:
    return "ltx-2-3-pro" if duration <= PRO_MAX_DURATION else "ltx-2-3-fast"


def _generate_video(prompt: str, duration: float, variant: str) -> bytes:
    load_model()
    raise NotImplementedError("wire actual LTX-2.3 inference call here")


def handler(job: dict) -> dict:
    try:
        prompt, duration = validate_input(job.get("input", {}))
    except ValueError as e:
        return {"error": str(e)}

    variant = select_model_variant(duration)
    video_bytes = _generate_video(prompt, duration, variant)
    key = f"clips/{uuid.uuid4().hex}.mp4"
    return {"output": {"key": key, "bytes_b64": base64.b64encode(video_bytes).decode("ascii")}}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd worker-video && python -m pytest tests/test_handler.py -v`
Expected: PASS

- [ ] **Step 5: Write `worker-video/requirements.txt`**

```
runpod==1.7.4
```

- [ ] **Step 6: Write `worker-video/Dockerfile`**

```dockerfile
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends python3.11 python3-pip && rm -rf /var/lib/apt/lists/*

WORKDIR /worker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt torch --index-url https://download.pytorch.org/whl/cu124

COPY handler.py .

# Model weights are NOT baked into this image — they load from the
# RunPod Network Volume mounted at /runpod-volume at container start.

CMD ["python3", "-u", "-c", "import runpod, handler; runpod.serverless.start({'handler': handler.handler})"]
```

- [ ] **Step 7: Commit**

```bash
git add worker-video/handler.py worker-video/Dockerfile worker-video/requirements.txt worker-video/tests/test_handler.py
git commit -m "feat: add LTX-2.3 RunPod worker handler with strict input validation"
```

---

## Task 18: Image Worker Handler (RunPod, FLUX.1-schnell)

**Files:**
- Create: `worker-image/handler.py`, `worker-image/Dockerfile`, `worker-image/requirements.txt`
- Test: `worker-image/tests/test_handler.py`

**Interfaces:**
- Produces: `handler.validate_input(job_input: dict) -> str` (returns validated prompt), `handler.handler(job: dict) -> dict`

- [ ] **Step 1: Write failing tests**

```python
# worker-image/tests/test_handler.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker-image && python -m pytest tests/test_handler.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `worker-image/handler.py`**

```python
# worker-image/handler.py
import base64
import uuid

RESOLUTION_WIDTH = 1920
RESOLUTION_HEIGHT = 1080
MODEL_VARIANT = "FLUX.1-schnell"

_MODEL = None


def load_model():
    global _MODEL
    if _MODEL is None:
        # Real FLUX.1-schnell weight load from the attached Network Volume
        # happens here. Integration point only — inference call not vendored.
        _MODEL = {"loaded": True}
    return _MODEL


def validate_input(job_input: dict) -> str:
    allowed_keys = {"prompt"}
    if set(job_input.keys()) - allowed_keys:
        raise ValueError(f"unexpected fields: {set(job_input.keys()) - allowed_keys}")
    if "prompt" not in job_input or not isinstance(job_input["prompt"], str) or not job_input["prompt"].strip():
        raise ValueError("prompt is required and must be a non-empty string")
    return job_input["prompt"]


def _generate_image(prompt: str) -> bytes:
    load_model()
    raise NotImplementedError("wire actual FLUX.1-schnell inference call here")


def handler(job: dict) -> dict:
    try:
        prompt = validate_input(job.get("input", {}))
    except ValueError as e:
        return {"error": str(e)}

    image_bytes = _generate_image(prompt)
    key = f"images/{uuid.uuid4().hex}.png"
    return {"output": {"key": key, "bytes_b64": base64.b64encode(image_bytes).decode("ascii")}}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd worker-image && python -m pytest tests/test_handler.py -v`
Expected: PASS

- [ ] **Step 5: Write `worker-image/requirements.txt`**

```
runpod==1.7.4
```

- [ ] **Step 6: Write `worker-image/Dockerfile`**

```dockerfile
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends python3.11 python3-pip && rm -rf /var/lib/apt/lists/*

WORKDIR /worker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt torch --index-url https://download.pytorch.org/whl/cu124

COPY handler.py .

CMD ["python3", "-u", "-c", "import runpod, handler; runpod.serverless.start({'handler': handler.handler})"]
```

- [ ] **Step 7: Commit**

```bash
git add worker-image/handler.py worker-image/Dockerfile worker-image/requirements.txt worker-image/tests/test_handler.py
git commit -m "feat: add FLUX.1-schnell RunPod worker handler with strict input validation"
```

---

## Task 19: Ops Guardrail Scripts

**Files:**
- Create: `ops/cost_alert.py`, `ops/scale_guard.py`, `ops/revenue_tracker.py`
- Test: `ops/tests/test_cost_alert.py`, `ops/tests/test_scale_guard.py`, `ops/tests/test_revenue_tracker.py`

**Interfaces:**
- Produces: `cost_alert.check_spend(runpod_api, webhook, limit_per_hour: float, threshold_pct: float) -> bool`, `scale_guard.check_and_fix_workers(runpod_api, endpoint_ids: list[str], target_max_workers: dict[str, int], webhook) -> dict`, `revenue_tracker.check_revenue(current_annual_revenue: float, threshold: float, webhook) -> bool`

- [ ] **Step 1: Write failing tests**

```python
# ops/tests/test_cost_alert.py
from unittest.mock import MagicMock
from cost_alert import check_spend


def test_alerts_when_spend_exceeds_threshold():
    runpod_api = MagicMock()
    runpod_api.get_current_hourly_spend.return_value = 60.0
    webhook = MagicMock()
    triggered = check_spend(runpod_api, webhook, limit_per_hour=80.0, threshold_pct=0.7)
    assert triggered is True
    webhook.send.assert_called_once()


def test_no_alert_when_spend_under_threshold():
    runpod_api = MagicMock()
    runpod_api.get_current_hourly_spend.return_value = 10.0
    webhook = MagicMock()
    triggered = check_spend(runpod_api, webhook, limit_per_hour=80.0, threshold_pct=0.7)
    assert triggered is False
    webhook.send.assert_not_called()
```

```python
# ops/tests/test_scale_guard.py
from unittest.mock import MagicMock
from scale_guard import check_and_fix_workers


def test_corrects_drifted_max_workers():
    runpod_api = MagicMock()
    runpod_api.get_endpoint_config.side_effect = lambda eid: {"max_workers": 2} if eid == "video-ep" else {"max_workers": 10}
    webhook = MagicMock()

    result = check_and_fix_workers(
        runpod_api, endpoint_ids=["video-ep", "image-ep"],
        target_max_workers={"video-ep": 15, "image-ep": 10}, webhook=webhook,
    )

    assert result["video-ep"]["corrected"] is True
    assert result["image-ep"]["corrected"] is False
    runpod_api.set_max_workers.assert_called_once_with("video-ep", 15)
    webhook.send.assert_called_once()


def test_no_correction_when_config_matches():
    runpod_api = MagicMock()
    runpod_api.get_endpoint_config.return_value = {"max_workers": 10}
    webhook = MagicMock()

    result = check_and_fix_workers(
        runpod_api, endpoint_ids=["image-ep"],
        target_max_workers={"image-ep": 10}, webhook=webhook,
    )

    assert result["image-ep"]["corrected"] is False
    runpod_api.set_max_workers.assert_not_called()
    webhook.send.assert_not_called()
```

```python
# ops/tests/test_revenue_tracker.py
from unittest.mock import MagicMock
from revenue_tracker import check_revenue


def test_flags_when_approaching_threshold():
    webhook = MagicMock()
    triggered = check_revenue(current_annual_revenue=9_000_000, threshold=10_000_000, webhook=webhook)
    assert triggered is True
    webhook.send.assert_called_once()


def test_no_flag_when_well_under_threshold():
    webhook = MagicMock()
    triggered = check_revenue(current_annual_revenue=1_000_000, threshold=10_000_000, webhook=webhook)
    assert triggered is False
    webhook.send.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ops && python -m pytest tests/ -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write `ops/cost_alert.py`**

```python
# ops/cost_alert.py
def check_spend(runpod_api, webhook, limit_per_hour: float, threshold_pct: float) -> bool:
    current_spend = runpod_api.get_current_hourly_spend()
    threshold = limit_per_hour * threshold_pct
    if current_spend >= threshold:
        webhook.send(
            f"RunPod hourly spend ${current_spend:.2f} has reached "
            f"{threshold_pct * 100:.0f}% of the ${limit_per_hour:.2f}/hr account limit."
        )
        return True
    return False
```

- [ ] **Step 4: Write `ops/scale_guard.py`**

```python
# ops/scale_guard.py
def check_and_fix_workers(runpod_api, endpoint_ids: list[str], target_max_workers: dict[str, int], webhook) -> dict:
    results = {}
    corrected_any = False
    for endpoint_id in endpoint_ids:
        config = runpod_api.get_endpoint_config(endpoint_id)
        target = target_max_workers[endpoint_id]
        current = config["max_workers"]
        if current != target:
            runpod_api.set_max_workers(endpoint_id, target)
            results[endpoint_id] = {"corrected": True, "was": current, "now": target}
            corrected_any = True
        else:
            results[endpoint_id] = {"corrected": False, "was": current, "now": current}

    if corrected_any:
        drifted = {k: v for k, v in results.items() if v["corrected"]}
        webhook.send(f"RunPod max_workers drift corrected: {drifted}")

    return results
```

- [ ] **Step 5: Write `ops/revenue_tracker.py`**

```python
# ops/revenue_tracker.py
def check_revenue(current_annual_revenue: float, threshold: float, webhook, warn_pct: float = 0.8) -> bool:
    if current_annual_revenue >= threshold * warn_pct:
        webhook.send(
            f"Annual revenue ${current_annual_revenue:,.2f} is approaching the "
            f"${threshold:,.2f} LTX-2.3 self-host-commercial license threshold. "
            f"Flag for legal review."
        )
        return True
    return False
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd ops && python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add ops/cost_alert.py ops/scale_guard.py ops/revenue_tracker.py ops/tests/
git commit -m "feat: add cost alert, scale-drift guard, and revenue threshold tracker scripts"
```

---

## Task 20: Full-Suite Verification + README

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: nothing new — this task verifies everything built in Tasks 1-19 together

- [ ] **Step 1: Run every test suite**

```bash
cd backend && python -m pytest -v
cd ../worker-video && python -m pytest -v
cd ../worker-image && python -m pytest -v
cd ../ops && python -m pytest -v
```

Expected: All PASS, zero failures across all four suites

- [ ] **Step 2: Validate docker-compose config once more**

Run: `docker compose config`
Expected: resolves without error

- [ ] **Step 3: Write root `README.md`**

```markdown
# Lantaw Generator

Self-hosted LTX-2.3 video and FLUX.1-schnell image generation service on RunPod Serverless.

## Components

- `backend/` — FastAPI proxy: auth, rate limiting, moderation, job queue, stitching
- `worker-video/` — RunPod handler for LTX-2.3 (prompt + duration in, video clip out)
- `worker-image/` — RunPod handler for FLUX.1-schnell (prompt in, 1080p image out)
- `ops/` — cost alert, RunPod scale-drift guard, revenue threshold tracker scripts

## Local development

1. Copy `backend/.env.example` to `backend/.env` and fill in real credentials.
2. Set `BACKEND_DOMAIN` and `POSTGRES_PASSWORD` in a root `.env` for docker-compose.
3. `docker compose up --build`

## Public API surface

- `POST /generate-image` — `{"prompt": "..."}`
- `POST /generate-video` — `{"prompt": "...", "target_duration": 30}`
- `GET /jobs/{id}`, `GET /projects/{id}` — status polling (ownership-checked)

No other fields are accepted. Resolution, fps, and model variant are fixed
server-side and never part of the public contract.

See `docs/superpowers/specs/2026-09-15-ltx-video-flux-image-service-design.md`
for the full design.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: add project README"
```

---

## Post-Plan Integration Notes (not automated tasks — manual follow-up)

- `_generate_video` in `worker-video/handler.py` and `_generate_image` in `worker-image/handler.py` are integration points left as `NotImplementedError` — wiring the actual LTX-2.3 and FLUX.1-schnell inference calls requires the real model repos/weights per spec Part 1, item 1, and is hardware-dependent (cannot be TDD'd without a GPU).
- RunPod Serverless endpoint creation (Part 2 config: GPU type, min/max workers, Network Volume attachment, Bearer auth) is done via the RunPod console/API directly — no code artifact, verify manually per spec Part 2.
- `docker-compose.yml`'s Caddy TLS requires a real domain pointed at the host — cannot be verified until deployed.
