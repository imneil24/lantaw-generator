# RunPod Webhook Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the in-RQ-job blocking poll loop against RunPod with a webhook-driven completion flow, so an RQ worker slot is held only for the instant it takes to submit a job to RunPod, not for the full duration of generation (including RunPod cold start).

**Architecture:** `RunpodClient.dispatch_video`/`dispatch_image` add a `webhook` URL to the RunPod `/run` request body and return immediately after submission instead of polling. `queue.py`'s `process_clip_job`/`process_image_job` persist `runpod_job_id`, set `Job.status = "dispatched"`, and return. A new FastAPI router (`app/webhooks.py`), mounted **without** the app's global auth/rate-limit dependencies, receives RunPod's callback, validates a path secret, and calls the same `_finish_clip_job`/`_finish_image_job`/failure-handling helpers `queue.py` already has. A new `ops/reconcile_stuck_jobs.py` cron script polls any job still `"dispatched"` past a time threshold, as a safety net for dropped webhook deliveries.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy, RQ (Redis Queue), httpx, pytest.

**Spec:** `docs/superpowers/specs/2026-09-18-runpod-webhook-completion-design.md`

## Global Constraints

- `extra="forbid"` and no caller-controlled URL fields on any public Pydantic request model — unaffected by this plan, but the new webhook route must not introduce one (the callback URL is server-constructed only, never client input).
- Every new secret field added to `Settings` must be added to `all_secrets()` for log redaction (`backend/app/logging_conf.py`).
- The webhook route must NOT inherit the app's global `auth_dependency`/`rate_limit_dependency` (see `backend/app/main.py:48`) — RunPod's callback carries no bearer key matching `backend_api_key_hash`. It is gated solely by the path secret.
- In-memory SQLite route tests need `poolclass=StaticPool` and `connect_args={"check_same_thread": False}` (see `backend/tests/test_routes_jobs.py` for the pattern).
- Follow existing code style: no docstrings except where explaining non-obvious WHY (see existing comments in `queue.py`/`runpod_client.py` as the bar), type-hinted function signatures, `logging.getLogger(__name__)` per module.

---

## File Structure

- Modify `backend/app/models.py` — add `Job.status = "dispatched"` as a valid value (no schema change needed beyond documenting it; `status` is already a free-form `String(16)`), add `Job.updated_at` column (needed by the reconciliation sweep's age filter).
- Modify `backend/app/config.py` — rename `webhook_url` → `public_base_url`, add `runpod_webhook_secret`, update `all_secrets()`.
- Modify `backend/.env.example` — rename `WEBHOOK_URL` → `PUBLIC_BASE_URL`, add `RUNPOD_WEBHOOK_SECRET`.
- Modify `backend/app/runpod_client.py` — `_dispatch` builds and sends the `webhook` field, stops polling after submit, returns a dispatched-sentinel; `dispatch_video`/`dispatch_image` gain `webhook_base_url`/`webhook_secret` wiring via constructor.
- Modify `backend/app/queue.py` — `process_clip_job`/`process_image_job` dispatch-and-return; new shared non-raising failure helper extracted for reuse by the webhook handler and `resume_orphaned_jobs`; `Job.updated_at` touched on every status transition.
- Create `backend/app/webhooks.py` — new router, `POST /webhooks/runpod/{secret}/{job_id}`.
- Modify `backend/app/main.py` — mount the webhooks router without global auth/rate-limit dependencies; wire its DB/runpod-finishing dependencies.
- Create `backend/tests/test_webhooks.py`.
- Modify `backend/tests/test_queue.py` — update dispatch tests for new dispatch-and-return behavior; keep orphan-resume tests as-is (unaffected).
- Modify `backend/tests/test_runpod_client.py` — update dispatch tests for webhook field + no-poll-after-submit behavior.
- Modify `backend/tests/test_main.py` — cover `PUBLIC_BASE_URL`/`RUNPOD_WEBHOOK_SECRET` env var wiring if `test_main.py` asserts on `Settings` construction (checked in Task 1).
- Create `ops/reconcile_stuck_jobs.py`.
- Create `ops/tests/test_reconcile_stuck_jobs.py`.

---

### Task 1: `Job` model gains `updated_at`; config rename + new secret

**Files:**
- Modify: `backend/app/models.py:10-21` (`Job` class)
- Modify: `backend/app/config.py` (whole file)
- Modify: `backend/.env.example`
- Test: `backend/tests/test_models.py`
- Test: `backend/tests/test_main.py` (check existing content first — see Step 1)

**Interfaces:**
- Produces: `Job.updated_at: Mapped[datetime]`, auto-set on insert, must be updated by callers on every status-changing commit (later tasks in `queue.py` and `webhooks.py` are responsible for setting it — this task only adds the column).
- Produces: `Settings.public_base_url: str` (renamed from `webhook_url`), `Settings.runpod_webhook_secret: str`.

- [ ] **Step 1: Read existing config/model tests to avoid breaking unrelated assertions**

Read `backend/tests/test_models.py` and `backend/tests/test_main.py` in full before editing — `test_main.py` is known to reference `WEBHOOK_URL` (see `monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.com/x")`), which must become `PUBLIC_BASE_URL`.

- [ ] **Step 2: Write the failing test for `Job.updated_at`**

Add to `backend/tests/test_models.py`:

```python
def test_job_has_updated_at_column_defaulting_to_creation_time():
    from datetime import datetime, timezone
    from app.models import Job
    job = Job(id="x", type="clip", prompt="p", duration=10.0, status="pending", retry_count=0)
    assert job.updated_at is None or isinstance(job.updated_at, datetime)
```

(If `test_models.py` doesn't exist yet or has a different structure, match its existing style — read it first per Step 1.)

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_models.py -k updated_at -v`
Expected: FAIL with `AttributeError: 'Job' object has no attribute 'updated_at'`

- [ ] **Step 4: Add the column**

In `backend/app/models.py`, add to the `Job` class after `created_at`:

```python
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_models.py -k updated_at -v`
Expected: PASS

- [ ] **Step 6: Rename `webhook_url` to `public_base_url`, add `runpod_webhook_secret` — write failing test first**

Update whatever existing test in `backend/tests/test_main.py` sets `WEBHOOK_URL` to instead set `PUBLIC_BASE_URL` and add `RUNPOD_WEBHOOK_SECRET`. If no test currently asserts on `Settings` fields directly, add one to a suitable existing test file (check `backend/tests/test_schemas.py` or create the assertion inline in `test_main.py` near the existing env setup):

```python
def test_settings_loads_public_base_url_and_runpod_webhook_secret(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("RUNPOD_VIDEO_KEY", "k")
    monkeypatch.setenv("RUNPOD_IMAGE_KEY", "k")
    monkeypatch.setenv("RUNPOD_VIDEO_ENDPOINT", "https://api.runpod.ai/v2/vid/runsync")
    monkeypatch.setenv("RUNPOD_IMAGE_ENDPOINT", "https://api.runpod.ai/v2/img/runsync")
    monkeypatch.setenv("R2_WRITE_KEY", "k")
    monkeypatch.setenv("R2_WRITE_SECRET", "s")
    monkeypatch.setenv("R2_READ_KEY", "k")
    monkeypatch.setenv("R2_READ_SECRET", "s")
    monkeypatch.setenv("R2_BUCKET", "b")
    monkeypatch.setenv("R2_ENDPOINT", "https://x.r2.cloudflarestorage.com")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("BACKEND_API_KEY_HASH", "hash")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("RUNPOD_WEBHOOK_SECRET", "s3cr3t")

    settings = Settings(_env_file=None)
    assert settings.public_base_url == "https://api.example.com"
    assert settings.runpod_webhook_secret == "s3cr3t"
    assert settings.runpod_webhook_secret in settings.all_secrets()
```

- [ ] **Step 7: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_main.py -k public_base_url -v`
Expected: FAIL (field doesn't exist / `webhook_url` required instead)

- [ ] **Step 8: Edit `backend/app/config.py`**

```python
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
    public_base_url: str
    runpod_webhook_secret: str
    moderation_api_key: str | None = None

    def all_secrets(self) -> list[str]:
        return [
            self.runpod_video_key, self.runpod_image_key,
            self.r2_write_secret, self.r2_read_secret,
            self.postgres_dsn, self.redis_url,
            self.backend_api_key_hash, self.runpod_webhook_secret,
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

Note `public_base_url` is deliberately left out of `all_secrets()` — it's a public URL, not a secret (unlike the old `webhook_url` which was arguably mislabeled). `runpod_webhook_secret` is added since it must be redacted from logs.

- [ ] **Step 9: Update `backend/.env.example`**

Replace `WEBHOOK_URL=https://hooks.example.com/changeme` with:

```
PUBLIC_BASE_URL=https://api.example.com
RUNPOD_WEBHOOK_SECRET=changeme
```

- [ ] **Step 10: Fix any other test referencing `WEBHOOK_URL`**

Search and update: `backend/tests/test_main.py:21` per the earlier grep finding — change `monkeypatch.setenv("WEBHOOK_URL", ...)` to `monkeypatch.setenv("PUBLIC_BASE_URL", ...)` and add `monkeypatch.setenv("RUNPOD_WEBHOOK_SECRET", "test-secret")` alongside it.

- [ ] **Step 11: Run full backend test suite to confirm no other breakage**

Run: `cd backend && python -m pytest -v`
Expected: All PASS (aside from tests this plan will touch in later tasks, which may still reference old behavior — if any unrelated test fails here due to the rename, fix it now).

- [ ] **Step 12: Commit**

```bash
git add backend/app/models.py backend/app/config.py backend/.env.example backend/tests/test_models.py backend/tests/test_main.py
git commit -m "feat: add Job.updated_at and rename webhook_url to public_base_url, add runpod_webhook_secret"
```

---

### Task 2: `RunpodClient` sends `webhook` on dispatch, stops polling after submit

**Files:**
- Modify: `backend/app/runpod_client.py` (whole file)
- Test: `backend/tests/test_runpod_client.py`

**Interfaces:**
- Consumes: `Settings.public_base_url`, `Settings.runpod_webhook_secret` (Task 1).
- Produces: `RunpodClient(settings, poll_interval=5, max_poll_attempts=240)` — constructor signature unchanged (still reads `public_base_url`/`runpod_webhook_secret` off `settings`, no new constructor params, keeping the existing `_fake_settings()` `MagicMock` pattern in tests working). `dispatch_video(prompt, duration, on_submitted=None) -> dict` and `dispatch_image(prompt, on_submitted=None) -> dict` now return `{"status": "dispatched", "runpod_job_id": str}` instead of a finished result. `poll_video`/`poll_image`/`_poll` signatures unchanged — still used by `resume_orphaned_jobs` and the new reconciliation sweep (Task 5).

- [ ] **Step 1: Write failing test for webhook field in dispatch payload**

Add to `backend/tests/test_runpod_client.py`, updating `_fake_settings()` first to include the two new fields:

```python
def _fake_settings():
    return MagicMock(
        runpod_video_key="video-key",
        runpod_image_key="image-key",
        runpod_video_endpoint="https://api.runpod.ai/v2/vid/runsync",
        runpod_image_endpoint="https://api.runpod.ai/v2/img/runsync",
        public_base_url="https://api.example.com",
        runpod_webhook_secret="s3cr3t",
    )
```

```python
@patch("app.runpod_client.httpx.post")
def test_dispatch_video_includes_webhook_url_scoped_to_the_job(mock_post):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})

    client = RunpodClient(_fake_settings())
    client.dispatch_video(prompt="a river at dawn", duration=8, job_id="our-job-42")

    args, kwargs = mock_post.call_args
    assert kwargs["json"]["webhook"] == "https://api.example.com/webhooks/runpod/s3cr3t/our-job-42"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_runpod_client.py -k webhook_url_scoped -v`
Expected: FAIL — `dispatch_video()` doesn't accept `job_id` yet, and no `webhook` key is sent.

- [ ] **Step 3: Write failing test for dispatch-and-return (no polling after submit)**

```python
@patch("app.runpod_client.httpx.get")
@patch("app.runpod_client.httpx.post")
def test_dispatch_video_returns_immediately_without_polling(mock_post, mock_get):
    mock_post.return_value = _mock_response(200, {"id": "job-1", "status": "IN_QUEUE"})

    client = RunpodClient(_fake_settings())
    result = client.dispatch_video(prompt="a river at dawn", duration=8, job_id="our-job-42")

    assert result == {"status": "dispatched", "runpod_job_id": "job-1"}
    mock_get.assert_not_called()
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_runpod_client.py -k returns_immediately -v`
Expected: FAIL — current `_dispatch` calls `self._poll(...)` and returns its result.

- [ ] **Step 5: Update existing tests that assumed polling-on-dispatch**

The following existing tests in `backend/tests/test_runpod_client.py` assert on the old poll-until-completion behavior and must be rewritten, since `dispatch_video`/`dispatch_image` no longer poll:

- `test_dispatch_video_polls_status_until_completed` — DELETE (this behavior moves entirely to `poll_video`, which is already tested by other means; dispatch itself no longer polls).
- `test_dispatch_video_raises_on_failed_status` — DELETE (dispatch can't observe FAILED anymore; failure-after-dispatch is now the webhook's concern, not `RunpodClient`'s).
- `test_dispatch_video_raises_on_poll_timeout` — DELETE (same reason).
- `test_dispatch_image_submits_and_polls` — RENAME/REWRITE to `test_dispatch_image_submits_and_returns_dispatched`:

```python
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
```

- `test_dispatch_video_passes_through_handler_error_on_completed_status` — DELETE (this tested `_poll`'s handling of a completed-but-errored handler result; that logic still exists in `_poll`/`_extract_output` for the webhook/reconciliation path, but is no longer exercised via `dispatch_video`. Coverage for it stays on `poll_video` — no new test needed here since `_poll` itself is untouched and its existing behavior is unchanged, just no longer reached through `dispatch_video`).
- `test_dispatch_video_calls_on_submitted_with_job_id_before_polling` — RENAME to `test_dispatch_video_calls_on_submitted_with_job_id_before_returning`, keep assertions as-is (still valid — `on_submitted` still fires with RunPod's job id right after submit).
- `test_dispatch_video_submits_to_run_endpoint` — keep, but add `job_id="j"` to the `dispatch_video` call (required positional/keyword now) and add an assertion the `webhook` key is present in the payload.
- `test_dispatch_video_sets_execution_timeout_policy_matching_poll_ceiling` — keep as-is; `execution_timeout_ms` computation is unchanged (still `poll_interval * max_poll_attempts * 1000`, per spec section 1 which keeps this constant, only decoupling it conceptually — no code change needed to this value in this task). Add `job_id="j"` to the call.
- `test_dispatch_video_raises_on_non_200_submit` — keep as-is, add `job_id="j"` to the call.
- `test_dispatch_video_does_not_call_on_submitted_when_submit_fails` — keep as-is, add `job_id="j"` to the call.

- [ ] **Step 6: Implement `_dispatch` changes**

Rewrite `backend/app/runpod_client.py`:

```python
import time
from typing import Callable

import httpx


class RunpodClient:
    # 240 attempts * 5s = 20 minutes: real LTX-2.5 video generation (text
    # encode, transformer denoise, spatial upscale, VAE decode) has been
    # observed taking 8-13 minutes end to end, so the previous 10-minute
    # ceiling raced RunPod's own executionTimeout and lost, aborting jobs
    # that were still running fine on RunPod's side. Dispatch itself no
    # longer polls against this ceiling (see dispatch_video/dispatch_image)
    # — it now bounds policy.executionTimeout and the reconciliation
    # sweep's/resume_orphaned_jobs's short capped polls instead.
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
        # explicitly overrides that default for this job. This value is no
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
```

- [ ] **Step 7: Run full test file to verify all pass**

Run: `cd backend && python -m pytest tests/test_runpod_client.py -v`
Expected: All PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/runpod_client.py backend/tests/test_runpod_client.py
git commit -m "feat: RunpodClient dispatch sends webhook url, returns immediately without polling"
```

---

### Task 3: Shared non-raising failure helper (extracted for webhook + sweep reuse)

**Files:**
- Modify: `backend/app/queue.py:60-71` (`_handle_failure`)
- Test: `backend/tests/test_queue.py`

**Interfaces:**
- Produces: `_mark_job_failed_non_raising(session, job, exc) -> None` — increments `retry_count`, sets `status="failed"`, commits, logs, does NOT re-raise. Used by `resume_orphaned_jobs`'s except branch (replacing its inline duplicate logic), and by the webhook handler (Task 4) and reconciliation sweep (Task 5) for the same purpose.

This extraction is small enough to fold into this task rather than a standalone one — it's setup for Task 4, not an independently meaningful deliverable on its own.

- [ ] **Step 1: Write failing test for the extracted helper**

Add to `backend/tests/test_queue.py`:

```python
def test_mark_job_failed_non_raising_does_not_reraise():
    from app.queue import _mark_job_failed_non_raising
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="dispatched", retry_count=1))
    session.commit()
    job = session.query(Job).filter_by(id=job_id).one()

    _mark_job_failed_non_raising(session, job, RuntimeError("boom"))  # must not raise

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "failed"
    assert updated.retry_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_queue.py -k mark_job_failed_non_raising -v`
Expected: FAIL with `ImportError: cannot import name '_mark_job_failed_non_raising'`

- [ ] **Step 3: Implement the helper and use it in `resume_orphaned_jobs`**

In `backend/app/queue.py`, add near `_handle_failure`:

```python
def _mark_job_failed_non_raising(session, job: Job, exc: Exception) -> None:
    """Marks a job failed without re-raising, for callers with no RQ retry mechanism.

    _handle_failure always re-raises because it runs inside an RQ job and
    RQ's own Retry(max=...) needs the exception to decide whether to
    re-enqueue. This variant is for callers that are NOT inside an RQ job —
    the webhook handler and the periodic/startup reconciliation sweeps —
    where there is no RQ retry to hand the exception to, and swallowing it
    here lets the caller continue processing other jobs/requests.
    """
    logger.exception("job_id=%s failed: %s", job.id, exc)
    job.retry_count += 1
    job.status = "failed"
    session.commit()
```

Then in `resume_orphaned_jobs`'s except branch (`backend/app/queue.py:250-261`), replace the inline body:

```python
            except Exception as exc:
                # Not running inside an RQ job (this is a one-shot startup
                # sweep), so there's no RQ retry mechanism to hand the
                # exception to.
                session.rollback()
                _mark_job_failed_non_raising(session, job, exc)
```

(Removing the now-duplicated inline `logger.exception`/`retry_count`/`status`/`commit` lines it replaces.)

- [ ] **Step 4: Run tests to verify pass**

Run: `cd backend && python -m pytest tests/test_queue.py -v`
Expected: All PASS, including the pre-existing `test_resume_orphaned_jobs_marks_failed_without_aborting_the_rest_of_the_sweep`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue.py backend/tests/test_queue.py
git commit -m "refactor: extract non-raising job-failure helper for reuse outside RQ context"
```

---

### Task 4: `queue.py` dispatch-and-return; webhook endpoint does the finishing

**Files:**
- Modify: `backend/app/queue.py` (`process_clip_job`, `process_image_job`)
- Create: `backend/app/webhooks.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_queue.py`
- Test: Create `backend/tests/test_webhooks.py`

**Interfaces:**
- Consumes: `RunpodClient.dispatch_video(prompt, duration, job_id, on_submitted)`, `dispatch_image(prompt, job_id, on_submitted)` (Task 2) — now require `job_id`. Consumes `_mark_job_failed_non_raising` (Task 3). Consumes `Job.status`, `Job.updated_at` (Task 1).
- Produces: `webhooks.router` (FastAPI `APIRouter`), `webhooks.get_db_session`, `webhooks.get_runpod_finisher_deps` — override-points for `main.py` wiring, following the same `get_db_session`-as-override-point pattern used in `routes/jobs.py`.
- Produces: `queue._finish_webhook_result(session, r2_client, job, payload: dict) -> None` — the function `webhooks.py` calls; internally dispatches to `_finish_clip_job`/`_finish_image_job`/`_mark_job_failed_non_raising` based on `job.type` and the RunPod payload's `status`, mirroring the dispatch-by-type logic `resume_orphaned_jobs` already has inline (Step 3 extracts that shared shape).

- [ ] **Step 1: Write failing tests for dispatch-and-return in `process_clip_job`/`process_image_job`**

Replace `test_process_clip_job_marks_complete_on_success` in `backend/tests/test_queue.py` — it currently asserts dispatch finishes the job inline, which is no longer true. Rewrite it (and the parallel image test) to assert dispatch-and-return:

```python
def test_process_clip_job_dispatches_and_returns_without_finishing():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="pending", retry_count=0))
    session.commit()

    def fake_dispatch_video(prompt, duration, job_id, on_submitted=None):
        if on_submitted is not None:
            on_submitted("runpod-job-abc")
        return {"status": "dispatched", "runpod_job_id": "runpod-job-abc"}

    runpod_client = MagicMock()
    runpod_client.dispatch_video.side_effect = fake_dispatch_video
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_clip_job(job_id)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "dispatched"
    assert updated.runpod_job_id == "runpod-job-abc"
    assert updated.result_key is None  # not finished — webhook finishes it
    r2_client.upload.assert_not_called()
```

```python
def test_process_image_job_dispatches_and_returns_without_finishing():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="image", prompt="p", duration=None, status="pending", retry_count=0))
    session.commit()

    def fake_dispatch_image(prompt, job_id, on_submitted=None):
        if on_submitted is not None:
            on_submitted("runpod-job-img-1")
        return {"status": "dispatched", "runpod_job_id": "runpod-job-img-1"}

    runpod_client = MagicMock()
    runpod_client.dispatch_image.side_effect = fake_dispatch_image
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_image_job(job_id)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "dispatched"
    assert updated.runpod_job_id == "runpod-job-img-1"
```

Delete `test_process_clip_job_persists_runpod_job_id_as_soon_as_submitted` — superseded by the above (same assertion, now the primary test).

Rewrite `test_process_clip_job_polls_instead_of_redispatching_when_runpod_job_id_already_set` (and its image counterpart) — a redelivered RQ job for a job already `"dispatched"` is now a no-op, not a poll:

```python
def test_process_clip_job_noop_when_already_dispatched():
    # Redelivered RQ job for one already sent to RunPod — the webhook (or
    # reconciliation sweep) owns finishing it now, not a redelivered
    # process_clip_job call.
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="dispatched",
                     retry_count=0, runpod_job_id="runpod-job-existing"))
    session.commit()

    runpod_client = MagicMock()
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_clip_job(job_id)

    runpod_client.dispatch_video.assert_not_called()
    runpod_client.poll_video.assert_not_called()
    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "dispatched"  # untouched
```

```python
def test_process_image_job_noop_when_already_dispatched():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="image", prompt="p", duration=None, status="dispatched",
                     retry_count=0, runpod_job_id="runpod-job-existing-img"))
    session.commit()

    runpod_client = MagicMock()
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_image_job(job_id)

    runpod_client.dispatch_image.assert_not_called()
    runpod_client.poll_image.assert_not_called()
    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "dispatched"
```

`test_process_clip_job_triggers_stitch_when_last_clip_in_project_completes` and `test_process_clip_job_does_not_stitch_when_sibling_clips_still_pending` currently drive stitching through `process_clip_job`'s dispatch path — since dispatch no longer finishes jobs, these must be rewritten to call `queue._finish_webhook_result` (or directly `_finish_clip_job`, which is unchanged) instead of `process_clip_job` to exercise the stitch trigger. Rewrite `test_process_clip_job_triggers_stitch_when_last_clip_in_project_completes`:

```python
def test_finishing_last_clip_in_project_triggers_stitch(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tts.NullTTSProvider.generate", lambda self, text, target_duration: str(tmp_path / "silence.wav"))
    session = _make_session()

    project_id = str(uuid.uuid4())
    session.add(VideoProject(id=project_id, target_duration=10.0, clip_count=2, status="pending"))

    job1_id = str(uuid.uuid4())
    job2_id = str(uuid.uuid4())
    session.add(Job(id=job1_id, type="clip", prompt="p", duration=10.0, status="complete",
                     retry_count=0, result_key="clips/a.mp4"))
    session.add(Job(id=job2_id, type="clip", prompt="p", duration=10.0, status="dispatched",
                     retry_count=0, runpod_job_id="rp-2"))
    session.add(ProjectClip(project_id=project_id, sequence_index=0, job_id=job1_id))
    session.add(ProjectClip(project_id=project_id, sequence_index=1, job_id=job2_id))
    session.commit()

    r2_client = MagicMock()
    r2_client.download.return_value = b"fake-clip-bytes"

    def fake_stitch(clip_paths, audio_path, output_path):
        open(output_path, "wb").write(b"final-video-bytes")

    from app.queue import _finish_clip_job
    job2 = session.query(Job).filter_by(id=job2_id).one()
    with patch("app.queue.stitch_project", side_effect=fake_stitch) as mock_stitch:
        _finish_clip_job(session, r2_client, job2, {"output": {"key": "clips/b.mp4"}})

    mock_stitch.assert_called_once()
    project = session.query(VideoProject).filter_by(id=project_id).one()
    assert project.status == "complete"
    assert project.final_result_key is not None
```

Delete `test_process_clip_job_does_not_stitch_when_sibling_clips_still_pending` as a `process_clip_job` test — this behavior is already covered by `_maybe_stitch_project`'s own direct tests (`test_process_clip_job_does_not_stitch_when_sibling_clips_still_pending` duplicates what `test_maybe_stitch_project_*` tests already cover once dispatch no longer calls the finisher). Keep the `_maybe_stitch_project` tests as-is — they call `_maybe_stitch_project` directly and are unaffected by this refactor.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_queue.py -v`
Expected: Multiple FAILs — `process_clip_job`/`process_image_job` still finish inline, `dispatch_video` mock signature mismatch (missing `job_id` positional arg in old call sites), `Job.status` still using old logic.

- [ ] **Step 3: Implement `queue.py` changes**

Modify `process_clip_job` (replacing lines 169-203):

```python
def process_clip_job(job_id: str) -> None:
    session, runpod_client, r2_client = _build_dependencies()
    try:
        try:
            job = session.query(Job).filter_by(id=job_id).one()
        except NoResultFound:
            return

        if job.status in ("complete", "dispatched"):
            # "complete": redelivered/duplicate job, already finished.
            # "dispatched": already sent to RunPod by a prior attempt — the
            # webhook (or reconcile_stuck_jobs.py's sweep) owns finishing
            # it now, not a redelivered process_clip_job call.
            return

        try:
            def _persist_dispatched(runpod_job_id: str) -> None:
                job.runpod_job_id = runpod_job_id
                job.status = "dispatched"
                session.commit()

            runpod_client.dispatch_video(
                prompt=job.prompt, duration=job.duration, job_id=job.id, on_submitted=_persist_dispatched,
            )
        except Exception as exc:
            _handle_failure(session, job, exc)
    finally:
        session.close()
```

Modify `process_image_job` (replacing lines 266-293):

```python
def process_image_job(job_id: str) -> None:
    session, runpod_client, r2_client = _build_dependencies()
    try:
        try:
            job = session.query(Job).filter_by(id=job_id).one()
        except NoResultFound:
            return

        if job.status in ("complete", "dispatched"):
            return

        try:
            def _persist_dispatched(runpod_job_id: str) -> None:
                job.runpod_job_id = runpod_job_id
                job.status = "dispatched"
                session.commit()

            runpod_client.dispatch_image(prompt=job.prompt, job_id=job.id, on_submitted=_persist_dispatched)
        except Exception as exc:
            _handle_failure(session, job, exc)
    finally:
        session.close()
```

Note `r2_client` is now unused in both functions' success path but still returned by `_build_dependencies()` and still needed — leave the unpacking as-is (`_finish_webhook_result`, added next, is what uses it; these two functions keep the `r2_client` local unused is fine since `_build_dependencies()` returns a 3-tuple used elsewhere too — actually simplify: since these functions no longer call any r2-consuming finisher, drop the unused local by unpacking `session, runpod_client, _ = _build_dependencies()` in both).

Revise both functions' first line to `session, runpod_client, _ = _build_dependencies()`.

Add the shared finishing dispatcher (used by both the webhook handler and the reconciliation sweep), placed after `_finish_image_job`:

```python
def _finish_webhook_result(session, r2_client, job: Job, status: str, payload: dict) -> None:
    """Finishes a job from a RunPod webhook callback or reconciliation poll.

    status is RunPod's top-level job status ("COMPLETED"/"FAILED"), payload
    is the full RunPod response body. Mirrors the by-type dispatch
    resume_orphaned_jobs already does inline, factored out so webhooks.py
    and ops/reconcile_stuck_jobs.py share one implementation instead of a
    third copy of this branching.
    """
    if status == "FAILED":
        _mark_job_failed_non_raising(session, job, RuntimeError(f"RunPod job failed: {payload.get('error', payload)}"))
        return

    result = {"output": payload.get("output")}
    try:
        if job.type == "clip":
            _finish_clip_job(session, r2_client, job, result)
        else:
            _finish_image_job(session, r2_client, job, result)
    except Exception as exc:
        session.rollback()
        _mark_job_failed_non_raising(session, job, exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_queue.py -v`
Expected: All PASS.

- [ ] **Step 5: Write failing tests for the webhook endpoint**

Create `backend/tests/test_webhooks.py`:

```python
import uuid
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import MagicMock
from app.models import Base, Job
from app.webhooks import router, get_db_session, get_r2_client

SECRET = "test-webhook-secret"


def _build_app_with_job(status="dispatched", runpod_job_id="rp-abc", job_type="clip"):
    app = FastAPI()
    app.include_router(router)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()

    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type=job_type, prompt="p", duration=10.0 if job_type == "clip" else None,
                     status=status, retry_count=0, runpod_job_id=runpod_job_id))
    session.commit()

    r2_client = MagicMock()
    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_r2_client] = lambda: r2_client
    return app, session, job_id, r2_client


def test_webhook_wrong_secret_returns_404_and_does_not_change_job():
    app, session, job_id, _ = _build_app_with_job()
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/wrong-secret/{job_id}",
        json={"id": "rp-abc", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}},
    )
    assert response.status_code == 404
    assert session.query(Job).filter_by(id=job_id).one().status == "dispatched"


def test_webhook_unknown_job_id_returns_404():
    app, _, _, _ = _build_app_with_job()
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{uuid.uuid4()}",
        json={"id": "rp-abc", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}},
    )
    assert response.status_code == 404


def test_webhook_runpod_job_id_mismatch_returns_404_and_does_not_change_job():
    app, session, job_id, _ = _build_app_with_job(runpod_job_id="rp-abc")
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{job_id}",
        json={"id": "rp-DIFFERENT", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}},
    )
    assert response.status_code == 404
    assert session.query(Job).filter_by(id=job_id).one().status == "dispatched"


def test_webhook_already_complete_job_is_a_noop():
    app, session, job_id, r2_client = _build_app_with_job(status="complete")
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{job_id}",
        json={"id": "rp-abc", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}},
    )
    assert response.status_code == 200
    r2_client.upload.assert_not_called()


def test_webhook_completed_clip_job_finishes_it():
    app, session, job_id, r2_client = _build_app_with_job(job_type="clip")
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{job_id}",
        json={"id": "rp-abc", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}},
    )
    assert response.status_code == 200
    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"
    assert updated.result_key == "clips/x.mp4"


def test_webhook_completed_image_job_finishes_it():
    app, session, job_id, r2_client = _build_app_with_job(job_type="image")
    r2_client.upload.return_value = "images/x.png"
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{job_id}",
        json={"id": "rp-abc", "status": "COMPLETED", "output": {"key": "images/x.png", "bytes_b64": "AA=="}},
    )
    assert response.status_code == 200
    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"


def test_webhook_failed_status_marks_job_failed():
    app, session, job_id, _ = _build_app_with_job(job_type="image")
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{job_id}",
        json={"id": "rp-abc", "status": "FAILED", "error": "OOM"},
    )
    assert response.status_code == 200
    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "failed"
    assert updated.retry_count == 1
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_webhooks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.webhooks'`

- [ ] **Step 7: Implement `backend/app/webhooks.py`**

```python
import hmac
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from app.models import Job
from app.queue import _finish_webhook_result

logger = logging.getLogger(__name__)
router = APIRouter()


def get_db_session():
    raise NotImplementedError("override in app wiring")


def get_r2_client():
    raise NotImplementedError("override in app wiring")


def get_webhook_secret() -> str:
    raise NotImplementedError("override in app wiring")


@router.post("/webhooks/runpod/{secret}/{job_id}")
async def runpod_webhook(secret: str, job_id: str, request: Request,
                          session=Depends(get_db_session), r2_client=Depends(get_r2_client),
                          expected_secret: str = Depends(get_webhook_secret)):
    # Constant-time compare: this path segment is the only gate on this
    # endpoint (RunPod's callback carries no bearer key), so a
    # timing-based secret recovery here would fully defeat it.
    if not hmac.compare_digest(secret, expected_secret):
        raise HTTPException(status_code=404)

    job = session.query(Job).filter_by(id=job_id).one_or_none()
    if job is None:
        raise HTTPException(status_code=404)

    body = await request.json()
    runpod_job_id = body.get("id")
    # Never trust the callback body's identity over our own lookup — the
    # secret alone doesn't bind this request to *this* job's specific
    # RunPod dispatch, only to our deploy in general.
    if job.runpod_job_id is not None and runpod_job_id != job.runpod_job_id:
        logger.warning("webhook job_id=%s runpod_job_id mismatch: got %s, expected %s",
                        job_id, runpod_job_id, job.runpod_job_id)
        raise HTTPException(status_code=404)

    if job.status in ("complete", "failed"):
        # RunPod may retry webhook delivery on transient failures on its
        # end — this must be idempotent, not an error.
        return {"ok": True}

    status = body.get("status")
    _finish_webhook_result(session, r2_client, job, status, body)
    return {"ok": True}
```

- [ ] **Step 8: Wire the router into `main.py`, excluded from global auth**

Read `backend/app/main.py` in full again before editing (already read above) to confirm the exact `dependencies=[Depends(auth_dependency), Depends(rate_limit_dependency)]` line and router-registration block.

Modify `backend/app/main.py`:

```python
from app.routes import generate_image, generate_video, jobs
from app import webhooks
from app.queue import process_clip_job, process_image_job, MAX_RETRIES
```

After the existing `app.include_router(jobs.router)` block, add:

```python
    # Mounted WITHOUT auth_dependency/rate_limit_dependency — RunPod's
    # webhook callback carries no bearer key matching backend_api_key_hash.
    # This router is gated solely by the path secret (see webhooks.py).
    app.include_router(webhooks.router)
```

And in the dependency-overrides block, add:

```python
    app.dependency_overrides[webhooks.get_db_session] = db_session_override
    app.dependency_overrides[webhooks.get_r2_client] = r2_override
    app.dependency_overrides[webhooks.get_webhook_secret] = lambda: settings.runpod_webhook_secret
```

Because `app = FastAPI(dependencies=[...])` applies those dependencies globally to the `FastAPI` instance itself (not per-router), `app.include_router(webhooks.router)` alone does NOT exclude it — every route on `app` still gets `auth_dependency`/`rate_limit_dependency` regardless of which router it came from. Confirm this by reading `FastAPI`'s dependency-resolution semantics: dependencies passed to the `FastAPI(...)` constructor apply to all routes app-wide, including routers added later, with no per-router opt-out short of using `APIRouter`'s own `dependencies=` (which adds, doesn't remove) or mounting a second `FastAPI` sub-application via `app.mount(...)` that does NOT share the parent's constructor-level dependencies.

**Because of this, the webhook router must be mounted as a separate sub-application, not `include_router`'d onto the main protected app:**

```python
    webhook_app = FastAPI()
    webhook_app.include_router(webhooks.router)
    webhook_app.dependency_overrides[webhooks.get_db_session] = db_session_override
    webhook_app.dependency_overrides[webhooks.get_r2_client] = r2_override
    webhook_app.dependency_overrides[webhooks.get_webhook_secret] = lambda: settings.runpod_webhook_secret
    app.mount("/webhooks", webhook_app)
```

With this mount, `webhooks.py`'s route path must change from `/webhooks/runpod/{secret}/{job_id}` to `/runpod/{secret}/{job_id}` (the `/webhooks` prefix is now supplied by the mount point) — go back and fix the `@router.post(...)` decorator in `backend/app/webhooks.py` accordingly, and fix `RunpodClient._dispatch`'s webhook URL construction in Task 2 stays the same (`{public_base_url}/webhooks/runpod/{secret}/{job_id}` — the external URL shape is unchanged, only the internal route-definition prefix moves).

Re-run `backend/tests/test_webhooks.py`'s `TestClient(app)` construction — since those tests build `app = FastAPI(); app.include_router(router)` directly (not through `create_app()`), they're unaffected by the mount-vs-include distinction in `main.py`; they still test the router in isolation correctly. Add one additional test to `backend/tests/test_main.py` (or a new assertion in `test_webhooks.py`) that exercises the *actual* `create_app()` output to confirm the webhook path is reachable without an `Authorization` header end-to-end:

```python
def test_webhook_route_via_create_app_does_not_require_auth_header(monkeypatch):
    # regression guard: app-level auth_dependency must not apply to the
    # mounted webhook sub-app.
    monkeypatch.setenv("RUNPOD_VIDEO_KEY", "k")
    monkeypatch.setenv("RUNPOD_IMAGE_KEY", "k")
    monkeypatch.setenv("RUNPOD_VIDEO_ENDPOINT", "https://api.runpod.ai/v2/vid/runsync")
    monkeypatch.setenv("RUNPOD_IMAGE_ENDPOINT", "https://api.runpod.ai/v2/img/runsync")
    monkeypatch.setenv("R2_WRITE_KEY", "k")
    monkeypatch.setenv("R2_WRITE_SECRET", "s")
    monkeypatch.setenv("R2_READ_KEY", "k")
    monkeypatch.setenv("R2_READ_SECRET", "s")
    monkeypatch.setenv("R2_BUCKET", "b")
    monkeypatch.setenv("R2_ENDPOINT", "https://x.r2.cloudflarestorage.com")
    monkeypatch.setenv("POSTGRES_DSN", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("BACKEND_API_KEY_HASH", "hash")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("RUNPOD_WEBHOOK_SECRET", "test-secret")

    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    client = TestClient(app)
    # No Authorization header at all — a request that would 401 on any
    # normal route (e.g. GET /jobs/{id}) must not 401 here.
    response = client.post("/webhooks/runpod/test-secret/00000000-0000-0000-0000-000000000000",
                            json={"id": "x", "status": "COMPLETED", "output": {}})
    assert response.status_code == 404  # 404 (unknown job), not 401 (auth)
    get_settings.cache_clear()
```

Check `backend/tests/test_main.py`'s existing style/fixtures first (Step 1 of Task 1 already required reading this file) — if it already has a monkeypatch-all-env-vars helper or fixture, reuse it instead of duplicating the full env var list inline here.

- [ ] **Step 9: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_webhooks.py tests/test_main.py -v`
Expected: All PASS.

- [ ] **Step 10: Run full backend suite**

Run: `cd backend && python -m pytest -v`
Expected: All PASS.

- [ ] **Step 11: Commit**

```bash
git add backend/app/queue.py backend/app/webhooks.py backend/app/main.py backend/tests/test_queue.py backend/tests/test_webhooks.py backend/tests/test_main.py
git commit -m "feat: dispatch-and-return job flow, add RunPod webhook completion endpoint"
```

---

### Task 5: Reconciliation sweep (`ops/reconcile_stuck_jobs.py`)

**Files:**
- Create: `ops/reconcile_stuck_jobs.py`
- Create: `ops/tests/test_reconcile_stuck_jobs.py`

**Interfaces:**
- Consumes: `RunpodClient.poll_video`/`poll_image` (unchanged signatures from Task 2), `queue._finish_webhook_result` (Task 4) — note `ops/` has no dependency on `backend/`'s package today (`ops/requirements.txt` is separate per `CLAUDE.md`'s repo-layout section); this task must NOT import from `app.*` directly. Instead, `reconcile_stuck_jobs` takes a `finish_result: Callable` and `mark_failed: Callable` as parameters, following the same "take an already-constructed client, return a bool/dict" pattern `ops/cost_alert.py` already uses — the actual wiring to `backend`'s `_finish_webhook_result`/`_mark_job_failed_non_raising` happens in the cron invocation script, not inside this testable function. Check `ops/scale_guard.py` for how it structures multi-step logic before finalizing this function's shape.
- Produces: `reconcile_stuck_jobs(session, runpod_client, finish_clip, finish_image, mark_failed, stale_before: datetime, poll_attempts: int = 6) -> dict` — returns a summary dict (e.g. `{"finished": N, "still_running": N, "failed": N}`) for the cron script to log, mirroring `ops/cost_alert.py`'s `bool`-return / `ops/scale_guard.py`'s `dict`-return convention (check `scale_guard.py` now to match whichever is closer).

- [ ] **Step 1: Read `ops/scale_guard.py` in full to match its structure**

(Already partially known from `ops/cost_alert.py`'s pattern — read `ops/scale_guard.py` before writing this task's implementation to confirm the dict-return convention and how it separates pure logic from the client objects it's given.)

- [ ] **Step 2: Write the failing test**

Create `ops/tests/test_reconcile_stuck_jobs.py`:

```python
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from reconcile_stuck_jobs import reconcile_stuck_jobs


class FakeJob:
    def __init__(self, id, type, status, runpod_job_id, updated_at):
        self.id = id
        self.type = type
        self.status = status
        self.runpod_job_id = runpod_job_id
        self.updated_at = updated_at


def _make_query(jobs):
    query = MagicMock()
    query.filter.return_value = query
    query.all.return_value = jobs
    return query


def test_reconcile_finishes_a_completed_stuck_job():
    now = datetime.now(timezone.utc)
    stuck_job = FakeJob("j1", "clip", "dispatched", "rp-1", now - timedelta(minutes=10))
    session = MagicMock()
    session.query.return_value = _make_query([stuck_job])

    runpod_client = MagicMock()
    runpod_client.poll_video.return_value = {"output": {"key": "clips/x.mp4"}}
    finish_clip = MagicMock()
    finish_image = MagicMock()
    mark_failed = MagicMock()

    summary = reconcile_stuck_jobs(
        session, runpod_client, finish_clip, finish_image, mark_failed,
        stale_before=now - timedelta(minutes=5),
    )

    finish_clip.assert_called_once_with(stuck_job, {"output": {"key": "clips/x.mp4"}})
    finish_image.assert_not_called()
    mark_failed.assert_not_called()
    assert summary == {"finished": 1, "still_running": 0, "failed": 0}


def test_reconcile_leaves_still_running_job_alone():
    now = datetime.now(timezone.utc)
    stuck_job = FakeJob("j1", "clip", "dispatched", "rp-1", now - timedelta(minutes=10))
    session = MagicMock()
    session.query.return_value = _make_query([stuck_job])

    runpod_client = MagicMock()
    runpod_client.poll_video.side_effect = TimeoutError("still running")
    finish_clip = MagicMock()
    finish_image = MagicMock()
    mark_failed = MagicMock()

    summary = reconcile_stuck_jobs(
        session, runpod_client, finish_clip, finish_image, mark_failed,
        stale_before=now - timedelta(minutes=5),
    )

    finish_clip.assert_not_called()
    mark_failed.assert_not_called()
    assert summary == {"finished": 0, "still_running": 1, "failed": 0}


def test_reconcile_marks_failed_on_runpod_error():
    now = datetime.now(timezone.utc)
    stuck_job = FakeJob("j1", "image", "dispatched", "rp-2", now - timedelta(minutes=10))
    session = MagicMock()
    session.query.return_value = _make_query([stuck_job])

    runpod_client = MagicMock()
    runpod_client.poll_image.side_effect = RuntimeError("RunPod job failed: OOM")
    finish_clip = MagicMock()
    finish_image = MagicMock()
    mark_failed = MagicMock()

    summary = reconcile_stuck_jobs(
        session, runpod_client, finish_clip, finish_image, mark_failed,
        stale_before=now - timedelta(minutes=5),
    )

    mark_failed.assert_called_once()
    assert mark_failed.call_args[0][0] is stuck_job
    assert summary == {"finished": 0, "still_running": 0, "failed": 1}


def test_reconcile_uses_short_poll_attempts_not_full_ceiling():
    now = datetime.now(timezone.utc)
    stuck_job = FakeJob("j1", "clip", "dispatched", "rp-1", now - timedelta(minutes=10))
    session = MagicMock()
    session.query.return_value = _make_query([stuck_job])

    runpod_client = MagicMock()
    runpod_client.poll_video.return_value = {"output": {"key": "clips/x.mp4"}}
    finish_clip = MagicMock()

    reconcile_stuck_jobs(
        session, runpod_client, finish_clip, MagicMock(), MagicMock(),
        stale_before=now - timedelta(minutes=5), poll_attempts=6,
    )

    runpod_client.poll_video.assert_called_once_with("rp-1", max_attempts=6)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ops && python -m pytest tests/test_reconcile_stuck_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'reconcile_stuck_jobs'`

- [ ] **Step 4: Implement `ops/reconcile_stuck_jobs.py`**

```python
from datetime import datetime
from typing import Callable

# job rows are whatever ORM object the caller's session.query returns —
# this module has no dependency on backend/'s SQLAlchemy models (ops/ and
# backend/ are independently-versioned components with separate
# requirements.txt files; see repo-layout notes), so it only relies on
# duck-typed .id/.type/.status/.runpod_job_id/.updated_at attributes.


def reconcile_stuck_jobs(session, runpod_client, finish_clip: Callable, finish_image: Callable,
                          mark_failed: Callable, stale_before: datetime, poll_attempts: int = 6) -> dict:
    """Recovers jobs whose RunPod webhook callback never arrived.

    Queries for jobs stuck at status="dispatched" older than stale_before,
    does a short bounded poll of RunPod's status for each (not the full
    ~20 minute ceiling — this is a safety net running on a cron cadence,
    not the primary completion path, so it must not block the cron job
    for a long time per stuck job), and finishes or fails them via the
    caller-supplied finish_clip/finish_image/mark_failed callbacks — kept
    as injected callables rather than importing backend's queue.py
    directly, since ops/ has no dependency on the backend package.
    """
    from app.models import Job  # deferred: only used for the filter below

    stuck_jobs = session.query(Job).filter(
        Job.status == "dispatched", Job.updated_at < stale_before,
    ).all()

    summary = {"finished": 0, "still_running": 0, "failed": 0}
    for job in stuck_jobs:
        poll = runpod_client.poll_video if job.type == "clip" else runpod_client.poll_image
        finish = finish_clip if job.type == "clip" else finish_image
        try:
            result = poll(job.runpod_job_id, max_attempts=poll_attempts)
        except TimeoutError:
            summary["still_running"] += 1
            continue
        except Exception as exc:
            mark_failed(job, exc)
            summary["failed"] += 1
            continue

        finish(job, result)
        summary["finished"] += 1

    return summary
```

Wait — the `from app.models import Job` deferred import inside the function body contradicts the "no dependency on backend package" interface note above. Fix this before implementing: `ops/requirements.txt` does not include the `backend` package, and `ops/pytest.ini`'s `pythonpath = .` only adds `ops/` itself to the path, not `backend/`. The query itself must also be injected rather than importing `Job`.

Revise the interface: caller passes a `query_stuck_jobs: Callable[[datetime], list]` instead of raw `session` + inline query construction:

```python
from datetime import datetime
from typing import Callable


def reconcile_stuck_jobs(query_stuck_jobs: Callable[[datetime], list], runpod_client,
                          finish_clip: Callable, finish_image: Callable, mark_failed: Callable,
                          stale_before: datetime, poll_attempts: int = 6) -> dict:
    """Recovers jobs whose RunPod webhook callback never arrived.

    query_stuck_jobs(stale_before) returns Job-like rows (duck-typed:
    .id/.type/.runpod_job_id) with status="dispatched" older than
    stale_before — injected rather than querying via SQLAlchemy directly
    here, since ops/ is an independently-versioned component with no
    dependency on the backend package (see repo-layout notes in
    CLAUDE.md). The actual query against backend's Job model lives in the
    invocation script that wires this function up, not here.

    Does a short bounded poll of RunPod's status for each stuck job (not
    the full ~20 minute ceiling — this is a safety net running on a cron
    cadence, not the primary completion path, so it must not block the
    cron job for a long time per stuck job).
    """
    stuck_jobs = query_stuck_jobs(stale_before)

    summary = {"finished": 0, "still_running": 0, "failed": 0}
    for job in stuck_jobs:
        poll = runpod_client.poll_video if job.type == "clip" else runpod_client.poll_image
        finish = finish_clip if job.type == "clip" else finish_image
        try:
            result = poll(job.runpod_job_id, max_attempts=poll_attempts)
        except TimeoutError:
            summary["still_running"] += 1
            continue
        except Exception as exc:
            mark_failed(job, exc)
            summary["failed"] += 1
            continue

        finish(job, result)
        summary["finished"] += 1

    return summary
```

And update the test file's `session.query(...)` mocking (Step 2 above) to instead mock a plain `query_stuck_jobs` callable rather than a SQLAlchemy session — replace every `session = MagicMock(); session.query.return_value = _make_query([stuck_job])` with `query_stuck_jobs = MagicMock(return_value=[stuck_job])`, drop the `_make_query` helper, and change every call site from `reconcile_stuck_jobs(session, runpod_client, ...)` to `reconcile_stuck_jobs(query_stuck_jobs, runpod_client, ...)`. Also assert `query_stuck_jobs.assert_called_once_with(stale_before)` in at least one test.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ops && python -m pytest tests/test_reconcile_stuck_jobs.py -v`
Expected: All PASS.

- [ ] **Step 6: Document the cron wiring (not executable in this repo's test suite, per CLAUDE.md's ops/ convention)**

Per `CLAUDE.md`: "Ops scripts are standalone, not wired into the backend... meant to be invoked by external cron." Add a `if __name__ == "__main__":` block to `ops/reconcile_stuck_jobs.py` showing the real wiring for a human deploying this (not covered by the unit test, matching how `cost_alert.py`/`scale_guard.py` are pure functions with the actual client construction left to the deploy-time entrypoint — check whether `cost_alert.py`/`scale_guard.py` have their own `__main__` blocks or a separate entrypoint file before adding one here, and match whichever pattern they use):

Check `ops/` directory for an existing `main.py` or entrypoint convention (glob `ops/*.py` results already gathered above show only `cost_alert.py`, `revenue_tracker.py`, `scale_guard.py` — no separate entrypoint file), so add a docstring-level comment (not a docstring, per this repo's no-comments-unless-non-obvious-WHY style) at the top of `reconcile_stuck_jobs.py` noting real wiring requires constructing a SQLAlchemy session against `backend`'s `Job` model, `RunpodClient` from `backend.app.runpod_client`, and `backend`'s `_finish_clip_job`/`_finish_image_job`/`_mark_job_failed_non_raising` — cross-package imports that only work if `ops`'s cron invocation sets `PYTHONPATH` to include `backend/` at deploy time, which is an infra/deploy concern outside this plan's scope (same category as the external cron trigger itself).

- [ ] **Step 7: Commit**

```bash
git add ops/reconcile_stuck_jobs.py ops/tests/test_reconcile_stuck_jobs.py
git commit -m "feat: add reconciliation sweep for jobs stuck without a webhook callback"
```

---

### Task 6: Full-suite verification and RQ job_timeout adjustment

**Files:**
- Modify: `backend/app/main.py:71` (`job_timeout=1500`)
- Test: run full suites, no new test file

**Interfaces:**
- None new — this task verifies the end-to-end change and tightens one now-stale constant.

- [ ] **Step 1: Reduce RQ `job_timeout`**

`backend/app/main.py:71`'s `job_queue.enqueue(target, job_id, retry=Retry(max=MAX_RETRIES), job_timeout=1500)` was sized to exceed `RunpodClient`'s old 1200s in-job poll loop. Since `process_clip_job`/`process_image_job` now dispatch-and-return (a single HTTP POST plus a DB commit — seconds, not 20 minutes), this can drop dramatically. Change to `job_timeout=120` (generous margin over a single `/run` POST + DB commit, matching the httpx client's own 30s timeout on that call with headroom for retries within `_dispatch` if RunPod's endpoint is briefly slow).

Update the comment above it:

```python
            # process_clip_job/process_image_job now dispatch-and-return
            # (a single RunPod /run POST + DB commit) rather than blocking
            # for the full generation duration — see webhooks.py for how
            # completion is now driven by RunPod's callback instead of an
            # in-job poll loop. 120s is generous headroom over the
            # dispatch call's own 30s httpx timeout.
            job_queue.enqueue(target, job_id, retry=Retry(max=MAX_RETRIES), job_timeout=120)
```

- [ ] **Step 2: Run full backend suite**

Run: `cd backend && python -m pytest -v`
Expected: All PASS.

- [ ] **Step 3: Run full ops suite**

Run: `cd ops && python -m pytest -v`
Expected: All PASS.

- [ ] **Step 4: Run worker-video and worker-image suites (unaffected, but confirm no accidental breakage)**

Run: `cd worker-video && python -m pytest -v`
Run: `cd worker-image && python -m pytest -v`
Expected: All PASS (this plan does not touch either handler).

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py
git commit -m "fix: shrink RQ job_timeout now that dispatch no longer blocks for full generation"
```

---

## Deferred / explicitly out of scope

- No RQ-level automatic retry on RunPod-side job failure post-refactor (see spec's "Open questions" section) — a `FAILED` webhook/reconciliation result marks the job failed with no re-enqueue. If this needs fixing, it's a follow-up: the webhook/sweep handler would need to call `job_queue.enqueue(...)` itself to re-dispatch, which needs the `Queue`/`Retry` objects threaded into `webhooks.py` and `ops/reconcile_stuck_jobs.py` — not addressed here.
- Actual external cron configuration (crontab entry, deploy-platform scheduled task) invoking `ops/reconcile_stuck_jobs.py` is an infra/deploy step, not code in this repo's test-covered surface — matches how `ops/cost_alert.py`/`scale_guard.py` are already handled.
- `RUNPOD_WEBHOOK_SECRET` generation/rotation process (e.g. via Railway's secret generation) is a deploy-time action, not implemented here — `.env.example` just documents the field.
