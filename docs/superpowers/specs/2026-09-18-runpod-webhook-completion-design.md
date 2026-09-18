# RunPod webhook-based completion (replaces hot-path polling)

Date: 2026-09-18
Status: approved, pending implementation plan

## Problem

`RunpodClient._dispatch` (backend/app/runpod_client.py) submits a job to
RunPod's `/run` endpoint, then blocks the calling RQ worker process in a
5s-interval HTTP poll loop against `/status/{job_id}` for up to 240 attempts
(20 minutes), inside the RQ job itself (`process_clip_job`/
`process_image_job` in backend/app/queue.py). This has two consequences:

1. **One RQ worker slot is held for the entire RunPod job duration**,
   including RunPod's own queue/cold-start time before inference even
   begins. With `workers=0` (scale-to-zero) on the RunPod endpoint, cold
   start eats into the same `policy.executionTimeout` budget (currently set
   equal to our poll ceiling: 1200s) that inference needs, raising the risk
   of RunPod killing the job as `executionTimeout exceeded` before
   generation finishes.
2. Polling is wasted HTTP traffic and wasted RQ concurrency for jobs that
   may sit in RunPod's queue for a while before a worker even picks them up.

RunPod Serverless supports a `webhook` field in the `/run` request body:
RunPod POSTs the job result to that URL once the job reaches a terminal
state, without the caller needing to poll. This removes the calling
process from the timing race entirely.

## Goals

- RQ job dispatches to RunPod and returns immediately; it does not hold a
  worker slot for the duration of generation.
- Job completion is driven by RunPod calling back into the backend, not by
  the backend polling RunPod.
- Existing finishing logic (R2 upload bookkeeping, stitching, retry/failure
  handling) is reused as-is, not duplicated.
- A dropped/never-delivered webhook cannot strand a job forever — a
  periodic reconciliation sweep recovers it.
- No change to the public API surface's SSRF posture (still no
  caller-controlled URLs anywhere in the request models).

## Non-goals

- Not changing `worker-video/handler.py` / `worker-image/handler.py`
  inference logic.
- Not removing `RunpodClient.poll_video`/`poll_image`/`_poll` — they remain,
  repurposed as the reconciliation sweep's mechanism (short bounded checks)
  instead of the hot-path completion mechanism.
- Not introducing a scheduler dependency (e.g. rq-scheduler) — the
  reconciliation sweep follows this repo's existing `ops/`
  external-cron-invoked-script convention.

## Design

### 1. Dispatch: add `webhook` to the RunPod `/run` payload

`RunpodClient._dispatch` (backend/app/runpod_client.py) gains a
`webhook_base_url` and `webhook_secret` (from `Settings`, see below) and
builds the callback URL per job:

```
{webhook_base_url}/webhooks/runpod/{webhook_secret}/{job_id}
```

`job_id` here is **our** `Job.id` (the DB primary key), not RunPod's job
id — the webhook handler needs to look up the DB row directly without
another round trip, and RunPod's own job id is provided separately in the
callback payload body for us to cross-check against what we persisted.

`body = {"input": ..., "webhook": callback_url, "policy": {"executionTimeout": ...}}`.

`_dispatch` no longer calls `self._poll(...)` after submission. It persists
`runpod_job_id` via the existing `on_submitted` callback, and additionally
returns a sentinel (e.g. `{"status": "dispatched"}`) rather than a finished
result — the caller (`queue.py`) uses this to know not to attempt
finishing.

`policy.executionTimeout` changes from `poll_interval * max_poll_attempts`
(a value that only made sense when the poll loop's own ceiling was the
thing to match) to a fixed, separately-configured max job duration
(reuse the same 1200s value as a constant, but decouple it in code from
`_poll`'s ceiling so the two can be tuned independently going forward).

### 2. `queue.py`: dispatch-and-return, new `"dispatched"` status

`process_clip_job` / `process_image_job`:

- If `job.runpod_job_id is None`: call `dispatch_video`/`dispatch_image`,
  set `job.status = "dispatched"` in the `on_submitted` callback (alongside
  persisting `runpod_job_id`), commit, **return** — do not call
  `_finish_clip_job`/`_finish_image_job`.
- If `job.runpod_job_id is not None` and `job.status == "dispatched"`: a
  redelivered RQ job for one already sent to RunPod — this is now a no-op
  (log and return), since the webhook (or reconciliation sweep) owns
  finishing it. This replaces today's inline `poll_video`/`poll_image` call
  in this branch.
- If `job.status == "complete"`: existing no-op guard, unchanged.

`Job.status` gains `"dispatched"` as a value between `"pending"` and
`"complete"`/`"failed"`. `resume_orphaned_jobs` (startup sweep, unchanged
in behavior) already keys off `status == "pending" and runpod_job_id is not
None` for the crash-before-persist case — that invariant still holds since
`status` only flips to `"dispatched"` *after* `runpod_job_id` is persisted
in the same commit. No change needed there.

### 3. New webhook endpoint

New file `backend/app/webhooks.py`, mounted into the FastAPI app in
`main.py`:

```
POST /webhooks/runpod/{secret}/{job_id}
```

- `secret` compared against `settings.runpod_webhook_secret` using
  constant-time comparison (`hmac.compare_digest`). Mismatch → 404 (not
  403 — avoid confirming the path shape exists to a prober).
- `job_id` looked up in `Job` table. Not found → 404.
- If `job.status` is already `"complete"` or `"failed"`: 200 no-op
  (RunPod may retry webhook delivery on transient failures on their end;
  this must be idempotent, not an error).
- If `job.status != "dispatched"`: 409-equivalent no-op-with-log (shouldn't
  happen; defensive).
- Parse RunPod's callback body (`status`, `output` / `error`, RunPod's own
  job `id`). Cross-check RunPod's job id against `job.runpod_job_id`;
  mismatch → log and 404 (never trust the body's identity over our own
  lookup).
- On `status == "COMPLETED"`: build the same `result` dict shape
  `_extract_output`/`_finish_clip_job`/`_finish_image_job` already expect,
  call the matching finisher (dispatch by `job.type`, same as
  `resume_orphaned_jobs` already does).
- On `status == "FAILED"`: call `_handle_failure`-equivalent logic. Note
  `_handle_failure` currently always re-raises (designed for RQ's retry
  machinery to catch) — the webhook handler is not inside an RQ job, so it
  needs the same non-raising variant `resume_orphaned_jobs`'s except
  branch already uses (increment `retry_count`, set `status="failed"`,
  commit, no re-raise). Extract that into a small shared helper instead of
  duplicating it a third time.
- Always returns 200 to RunPod once handled (or no-op'd) so RunPod doesn't
  retry a call we've already processed.

This endpoint is unauthenticated beyond the path secret (no bearer key —
RunPod's webhook caller doesn't send one). It is a new inbound trust
boundary; the path secret is the only gate, so it must be a
high-entropy random value, not a guessable string, and never logged.

### 4. Config

`backend/app/config.py`:

- Repurpose the currently-unused `webhook_url: str` field: rename to
  `public_base_url: str` (the backend's own externally-reachable base URL,
  used to build the callback URL — this is a fundamentally different thing
  than what `webhook_url` sounds like it was for, so renaming avoids
  confusion for the next reader).
- Add `runpod_webhook_secret: str` — high-entropy random string, generated
  once per deploy, added to `all_secrets()` for log redaction and to
  `.env.example`.

### 5. Reconciliation sweep

New `ops/reconcile_stuck_jobs.py`, following `ops/cost_alert.py`'s
existing pattern (standalone script, takes an already-constructed client,
meant for external cron — not imported by the FastAPI app).

- Queries `Job` rows with `status == "dispatched"` and `runpod_job_id is
  not None`, filtered to ones older than a threshold (e.g.
  `updated_at < now() - 5 minutes`) so in-flight jobs aren't touched.
- For each: calls `poll_video`/`poll_image` with a short `max_attempts`
  (same `RESUME_POLL_ATTEMPTS`-style short bounded check
  `resume_orphaned_jobs` uses — this is a safety net, not the hot path, so
  it must not block cron for a long time per stuck job).
- `TimeoutError` (still running) → leave alone, skip.
- Completed → call the same finisher as the webhook path.
- Failed/error → same non-raising failure helper as above.

Intended cron cadence: every 5 minutes (external cron, not in-process).

### 6. `RunpodClient._poll` / poll ceiling

Stays as the mechanism for both `resume_orphaned_jobs` and the new
reconciliation sweep — both already call it with a short `max_attempts`
override, unchanged. `_max_poll_attempts`'s default (240) becomes
effectively unused directly (no more full-length hot-path poll calls it
unconditionally), but is left as the class default rather than removed, to
avoid an API break for any direct caller and because it still documents
"this is RunPod's realistic worst-case job duration" for the
`executionTimeout` constant to reference.

## Data flow (happy path)

```
POST /generate-video
  → Job(status="pending") created, enqueued
RQ worker: process_clip_job
  → dispatch_video(..., on_submitted=persist_and_mark_dispatched)
  → RunPod POST /run {input, webhook: .../webhooks/runpod/{secret}/{job.id}, policy}
  → job.status = "dispatched", runpod_job_id persisted
  → RQ job returns (worker slot freed)

[RunPod runs the job on its own schedule/hardware]

RunPod POST /webhooks/runpod/{secret}/{job.id} {status: COMPLETED, output, id}
  → secret + job_id + runpod_job_id cross-check
  → _finish_clip_job / _finish_image_job (same as today)
  → 200 OK
```

## Error handling summary

| Scenario | Handling |
|---|---|
| Webhook secret mismatch | 404, no DB change |
| Job id not found | 404 |
| RunPod's job id in payload ≠ `job.runpod_job_id` | log + 404, no DB change |
| Job already `complete`/`failed` (duplicate webhook delivery) | 200 no-op |
| Webhook never arrives | reconciliation sweep (`ops/reconcile_stuck_jobs.py`) recovers it on next cron run |
| RunPod reports `FAILED` via webhook | shared non-raising failure helper: `retry_count += 1`, `status = "failed"` (no RQ retry available from webhook context — the RQ job already exited successfully after dispatch, so RunPod-side failure does not get RQ's `Retry(max=...)` re-enqueue; this is a behavior change from today, see Open questions) |
| Worker container crashes after dispatch, before webhook | existing `resume_orphaned_jobs` startup sweep + the new periodic sweep both cover this (status stays `"dispatched"`, `runpod_job_id` is set) |

## Open questions / accepted trade-offs

- **Loss of RQ-level retry on RunPod-side failure.** Today, a `FAILED`
  result inside the RQ job raises, and RQ's `Retry(max=3)` re-enqueues the
  whole job (fresh `/run` dispatch on retry, since `runpod_job_id` guard
  only prevents duplicate dispatch while `status` isn't yet terminal —
  actually today it always polls the same job on retry, never re-dispatches
  once `runpod_job_id` is set). Post-refactor, a `FAILED` webhook only sets
  `job.status = "failed"` directly with no RQ re-enqueue, since the
  originating RQ job already exited after dispatch. This matches this
  repo's existing `resume_orphaned_jobs` failure path (which also has no
  RQ retry available), so it's a pre-existing pattern, not a new one — but
  it does mean clip/image jobs that fail on RunPod's side no longer get an
  automatic retry at all. If automatic retry-on-RunPod-failure is wanted,
  it would need to be re-enqueued explicitly from the webhook/sweep handler
  (a follow-up, not in this spec's scope).

## Testing

- `backend/tests/test_webhooks.py` (new): valid secret + completed payload
  finishes job; invalid secret → 404 and no DB change; unknown job_id →
  404; runpod_job_id mismatch → 404 and no DB change; already-complete job
  → 200 no-op, no double-upload/double-stitch; FAILED payload → job marked
  failed.
- `backend/tests/test_queue.py` (update): `process_clip_job`/
  `process_image_job` now assert dispatch-and-return (status becomes
  `"dispatched"`, no finisher called, no poll call) instead of the old
  poll-to-completion assertions. Redelivery-while-`"dispatched"` case
  asserted as a no-op.
- `ops/tests/test_reconcile_stuck_jobs.py` (new): stuck job past threshold
  gets polled and finished; still-running job left alone; fresh
  (not-yet-old-enough) dispatched job not touched.
