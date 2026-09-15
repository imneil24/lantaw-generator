# Project: Self-Hosted LTX-2.3 Video Generation Service on RunPod

## Goal
Build a secure backend service exposing a minimal API — `prompt` + `duration` in,
video out — backed by a self-hosted LTX-2.3 model on RunPod Serverless. Service
generates short video clips stitched into longer narrative b-roll videos
(target: 30+ min final video). Caller controls only prompt and clip duration;
every other generation parameter (resolution, fps, model variant) stays fixed
server-side, never exposed in the API contract.

## Architecture
Client app -> Our backend API (auth, rate limiting, job orchestration) ->
RunPod Serverless endpoint (LTX-2.3 worker) -> Object storage (clip outputs) ->
Stitching/post-processing -> Final video

## Part 1: RunPod Worker Setup

1. Fork/adapt existing LTX-2.3 RunPod worker instead of building from scratch.
   Reference: github.com/199-biotechnologies/runpod-ltx2-worker
2. Write `handler.py` using RunPod Python SDK (`runpod` package). Handler must:
   - Load LTX-2.3 model ONCE at container startup (global scope, not per-request)
   - Accept ONLY these input fields from the request payload:
     - `prompt` (string, required)
     - `duration` (number, required — seconds)
   - Validate `duration` against LTX-2.3's allowed values before dispatch:
     - Pro tier: up to 10s — keeps retake/extend/reframe repair tools available
     - Fast tier: up to 20s — drops those repair tools
     - Reject/clamp out-of-range values; pick model variant (`ltx-2-3-pro` vs
       `ltx-2-3-fast`) based on which tier the requested duration falls into
   - Hardcode all other generation parameters server-side (never caller-supplied):
     - resolution: 1080p
     - fps: 24
   - Return the generated video file (or a URL to it in object storage)
3. Write a Dockerfile that:
   - Installs CUDA, PyTorch, and LTX-2.3 dependencies
   - Does NOT bake model weights into the image (weights load from a network
     volume — see Part 2)
4. Push image to Docker Hub/GHCR, OR connect GitHub repo directly to RunPod for
   auto-build-on-push (prefer this if using GitHub).

## Part 2: RunPod Endpoint Configuration

Create a new Serverless endpoint in the RunPod console/API with:
- GPU type: RTX 5090
- Min workers: 0 (scale to zero when idle)
- Max workers: 10–20 (adjust based on burst needs)
- Idle timeout: 5 seconds (default — only change if traffic pattern is steady)
- Attach a persistent Network Volume containing LTX-2.3 model weights, mounted
  so the container loads from the volume instead of re-downloading on every
  cold start
- Confirm the endpoint requires a Bearer API key on every request (RunPod's
  default auth) — test that unauthenticated requests are rejected

## Part 3: Backend Proxy Service (build this — do not skip)

Build a small backend service (Node/Python/whatever fits the existing stack) that:
- Holds the RunPod API key server-side ONLY — never exposed to any client app
- Exposes a single minimal endpoint, e.g. `POST /generate-clip`:
  - Request body: `{ "prompt": "...", "duration": 10 }`
  - Validates `duration` against allowed range before forwarding (defense in
    depth — do not rely solely on the worker-side check)
  - Applies our own auth (API key or session token) and rate limiting
  - Forwards the request to the RunPod endpoint with the RunPod Bearer key attached
  - Returns a job ID or the resulting clip URL to the caller
- Implements a job queue to:
  - Split a target video (e.g. 30 min) into clip generation jobs sized by the
    requested duration (e.g. ~180 jobs at 10s/clip)
  - Dispatch jobs to the RunPod endpoint (respecting max worker concurrency)
  - Retry failed or flagged-bad clips
  - Track job status until all clips for a video are complete

## Part 4: Post-Processing Pipeline

- Stitch completed clips into a single video file in the correct order
- Mux in a separately-generated TTS audio track (LTX-2.3 used WITHOUT native
  audio in this config — audio handled by a separate TTS step, not the model)
- Upload the final video to object storage (S3-compatible — RunPod's built-in
  storage or an external bucket)

## Part 5: Operational Guardrails (implement as monitoring/alerts, not just code)

- Cost alert if hourly spend approaches RunPod's default $80/hour account-wide
  spend limit
- Reminder/check tied to RunPod's auto-scale-down behavior: an endpoint with no
  requests for 3 days has its max workers auto-reduced to 2, and after 7 days
  to 0. If this service runs on a non-daily schedule, add a check before each
  production run to confirm max workers is still set correctly.
- Track cumulative revenue against LTX-2.3's license threshold: the model is
  free to self-host commercially only while annual revenue stays under $10M.
  Flag this for review if the business approaches that figure.

## Out of scope for this task
- Do not implement Wan 2.2 or any other model — LTX-2.3 only for now
- Do not expose resolution/fps/model-variant in the public-facing API contract
  — these stay hardcoded server-side per Part 1
- No image-to-video mode — text-to-video only, `prompt` + `duration` are the
  entire public parameter surface
