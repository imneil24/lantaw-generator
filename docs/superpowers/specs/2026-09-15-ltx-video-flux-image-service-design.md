# Design: Self-Hosted LTX-2.3 Video + FLUX.1 Image Generation Service

Date: 2026-09-15
Status: Approved for planning

## Goal

Backend service exposing minimal APIs — `prompt`+`duration` for video,
`prompt` for images — backed by self-hosted LTX-2.3 (video) and FLUX.1-schnell
(image) models on RunPod Serverless. Video clips stitch into long-form b-roll
(target 30+ min). Security is the primary non-functional requirement: no
caller-controlled parameter may reach infrastructure in an unvalidated form,
no secret may leak via logs/errors, and no unauthenticated request may reach
either RunPod endpoint.

## Architecture

```
Client app -> Backend API (FastAPI: auth, rate-limit, moderation, queue)
   -> RunPod Serverless (LTX-2.3 worker)   -> R2 (clips)
   -> RunPod Serverless (FLUX.1-schnell worker) -> R2 (images)
Backend stitcher (RQ worker, ffmpeg, CPU-only)
   -> pulls clips from R2 in order -> mux stub silent audio -> R2 (final video)
ops/ cron scripts -> RunPod API (cost + scale-guard) -> alert webhook
```

Two independent RunPod Serverless endpoints (video, image) — isolated blast
radius, separate Bearer keys, separate scale configs.

## Part 1: RunPod Workers

### Video worker (`worker-video/`)
- Fork base: github.com/199-biotechnologies/runpod-ltx2-worker
- `handler.py`: loads LTX-2.3 once at global scope on container start
- Input schema (strict, extra fields rejected):
  - `prompt: str` (required, max length enforced)
  - `duration: float` (required, range 1–20)
- Duration routing: `duration <= 10` → `ltx-2-3-pro` (keeps retake/extend/reframe
  tools); `10 < duration <= 20` → `ltx-2-3-fast` (repair tools unavailable,
  caller not informed of this trade-off at API level — internal routing only)
- Hardcoded, never caller-supplied: resolution 1080p, fps 24
- No `image_url` / no URL-shaped input fields anywhere in this handler —
  eliminates SSRF via the worker entirely
- Output: uploads clip to R2, returns R2 object key (not a public URL)

### Image worker (`worker-image/`)
- Model: FLUX.1-schnell (Apache 2.0 — no commercial revenue threshold, unlike
  FLUX.1-dev)
- `handler.py`: loads model once at global scope
- Input schema: `prompt: str` only (required, max length enforced)
- Hardcoded: output resolution 1080p (1920x1080)
- Output: uploads image to R2, returns R2 object key

### Shared worker practices
- Dockerfile: CUDA + PyTorch + model deps; weights NOT baked into image —
  loaded from attached RunPod Network Volume
- GitHub repo connected directly to RunPod for auto-build-on-push (no
  Docker Hub/GHCR credential to manage)
- Handlers never log the RunPod API key or full request payload verbatim —
  log prompt hash + duration only, not raw prompt text (moderation-flagged
  prompts are the one exception, logged server-side on the backend for audit)

## Part 2: RunPod Endpoint Configuration (both endpoints)

- GPU: RTX 5090
- Min workers: 0, Max workers: 10–20 (video), 5–10 (image, lighter load
  expected — adjust after real traffic data)
- Idle timeout: 5s default
- Network Volume attached per endpoint with respective model weights
- Bearer API key required (RunPod default) — verified via test call during
  setup that unauthenticated requests get rejected
- Each endpoint's Bearer key stored as a distinct secret in the backend's
  secret store — video worker compromise does not expose image worker key

## Part 3: Backend Proxy Service (FastAPI)

### Security spine

**Perimeter**
- Caller auth: single trusted client, `Authorization: Bearer <key>` header.
  Key stored as a salted hash (bcrypt) in the DB or env; comparison via
  `hmac.compare_digest` — never `==` — to prevent timing attacks
- TLS terminated at a reverse-proxy sidecar (Caddy) with automatic cert —
  the FastAPI app itself is never exposed on plaintext HTTP externally
- Rate limiting: per-key sliding window in Redis (e.g. 10 req/min for
  generate endpoints, higher for status polls), 429 on breach
- Input validation: Pydantic models with `extra="forbid"` on every request
  body — unknown fields rejected outright, not silently ignored
- No URL-typed input fields anywhere in the public API — removes SSRF
  surface at the perimeter, not just in the worker
- Generic error responses: 4xx/5xx bodies never include stack traces,
  internal paths, or upstream (RunPod) error detail — a correlation ID is
  returned to the caller and the full detail logged server-side only

**Secrets**
- RunPod keys (video + image), R2 credentials, Postgres/Redis DSNs, webhook
  URL: env-var only, never committed. `.env` gitignored, `.env.example`
  committed with placeholder values
- Logging filter (applied globally to the logger config) redacts any log
  record whose message contains a known secret value, sourced from the
  same env vars at startup
- R2 credentials are scoped: one write-only key for the worker upload path,
  one separate read-scoped key used only to mint short-lived signed GET
  URLs for finished results — neither key can list or delete the bucket
- No secret ever appears in a URL query string (avoids access-log leakage)

**Abuse / moderation**
- Every `prompt` passed through a moderation check before a job is created
  — external moderation API call if available, keyword-blocklist fallback
  otherwise. Rejected prompts return 422 and are never forwarded to RunPod
- Moderation-blocked attempts are logged (with prompt text, for audit) at a
  higher retention than normal request logs

**Job isolation**
- All job/project IDs are UUIDv4 — no sequential or guessable identifiers
- Status-poll endpoints check `api_key_id` ownership before returning any
  data — a caller cannot poll or discover another caller's job by ID guess

### Endpoints

- `POST /generate-image` — `{prompt}` → creates job, enqueues, returns job ID
- `POST /generate-video` — `{prompt, target_duration}` → computes clip count
  (target_duration / 10, Pro-tier default), creates `video_projects` +
  N `jobs` rows in one DB transaction, enqueues N clip jobs
- `GET /jobs/{id}` — single job status (image or individual clip)
- `GET /projects/{id}` — project status, per-clip progress, final R2 signed
  URL once stitching completes

### Data model (Postgres)
- `jobs`: id (UUID), api_key_id, type [clip|image], prompt, duration
  (nullable for image), status, retry_count, result_key, created_at
- `video_projects`: id (UUID), api_key_id, target_duration, clip_count,
  status, final_result_key
- `project_clips`: project_id, sequence_index, job_id — ordering by
  sequence_index, never by array/insertion order

### Queue
- Redis + RQ. Worker pool sized to match RunPod max_workers so the backend
  never fans out more concurrent RunPod calls than the endpoint can serve
- Backpressure: reject new `/generate-video` requests with 503 if current
  queue depth exceeds a configured ceiling, rather than accepting unbounded
  backlogs
- Retry: exponential backoff, max 3 attempts per job; final failure marks
  the job `failed` and the project `partial_failure` (never silently
  dropped — surfaced via `GET /projects/{id}`)

## Part 4: Post-Processing Pipeline

- `stitcher` RQ worker triggers once every clip in a project is `complete`
- Pulls clips from R2 in `sequence_index` order, concatenates via ffmpeg
- Audio: `TTSProvider` interface defined now (`generate(text) -> audio path`)
  with a `NullTTSProvider` implementation that returns a matching-length
  silent track. Real provider (e.g. ElevenLabs) can be swapped in later
  without touching stitching logic. **No real TTS is wired in this phase —
  output video has a silent audio track.**
- Final video uploaded to R2; project marked `complete`; signed URL made
  available via `GET /projects/{id}`
- Runs on the backend's own CPU container, not RunPod GPU (cost efficiency)
- Idempotent: stitch failure retries the whole stitch step (re-pull, re-
  concat) without re-generating any clips

## Part 5: Operational Guardrails

- `ops/cost_alert.py` (cron, ~15min): polls RunPod API for current spend
  rate, alerts via webhook at 70% of the $80/hr account-wide limit
- `ops/scale_guard.py` (cron, daily + pre-batch-run check): verifies both
  endpoints' max_workers still match configured target; RunPod auto-reduces
  to 2 after 3 idle days and 0 after 7 — this script detects and re-corrects
  drift, and must also be run manually before any non-routine production
  batch job
- `ops/revenue_tracker.py` (monthly, manual trigger): reads a revenue figure
  from a configured source (env var or small input file — not guessed),
  flags for legal/licensing review as projected annual revenue approaches
  LTX-2.3's $10M self-host-commercial threshold. FLUX.1-schnell (Apache 2.0)
  has no equivalent threshold and is not tracked here
- All alerts route to one webhook (Slack/Discord/email — chosen at
  implementation time), URL stored as an env-var secret like any other
  credential

## Out of Scope

- Real TTS provider integration (interface only, `NullTTSProvider` stub)
- FLUX.1-dev or any other image model (schnell only, avoids a second
  revenue-threshold license to track)
- Wan 2.2 or any video model beyond LTX-2.3
- Multi-tenant per-user auth (single trusted client, one API key)
- Exposing resolution/fps/model-variant/image_url in any public API contract

## Deployment

- `docker-compose.yml`: FastAPI app, Redis, Postgres, Caddy (TLS), RQ workers
  (dispatch + stitcher) — cloud-agnostic container deploy (Fly.io, Railway,
  ECS, VPS, etc.)
