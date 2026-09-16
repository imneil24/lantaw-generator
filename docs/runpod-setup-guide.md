# RunPod Setup Guide

Manual console/API steps to stand up the RunPod Serverless endpoints
(video, image). Not code — RunPod endpoint creation is deliberately done
outside the repo (spec: `docs/superpowers/specs/2026-09-15-ltx-video-flux-image-service-design.md`
Part 2).

**Video and image are independent — deploy video only for now, add
image later.** Nothing in the backend or either worker requires both
to exist at once (two separate RunPod keys/endpoints in
`backend/app/config.py`, dispatched independently by `runpod_client.py`).
Skip every image-specific step below until you're ready for it; just
don't call `POST /generate-image` (or leave `RUNPOD_IMAGE_*` unset) in
the meantime.

## Prerequisites

- RunPod account with billing configured
- GitHub repo pushed, containing `worker-video/` (and `worker-image/`
  once you get to image — each has its own `Dockerfile` + `handler.py`
  + `requirements.txt`)
- Model weights for LTX-2.3 (video) ready to upload to a Network Volume
  — weights are NOT baked into the Docker image (see the Dockerfile's
  comment). FLUX.1-schnell (image) weights only needed when you deploy
  that endpoint later.

## 1. Create a Network Volume (video first; image later)

1. RunPod console → Storage → Network Volumes → New Volume
2. Pick a datacenter region with RTX 5090 availability
3. Size to fit the model weights with headroom
4. One volume is enough while only deploying video. When you later add
   the image endpoint, create a **second, separate** volume for it —
   **do not share one volume between video and image endpoints**
5. Upload weights to the volume (RunPod's file browser, or a temporary
   pod attached to the volume + `scp`/`rsync`)

## 2. Create the Serverless endpoint (video now; repeat for image later)

RunPod console → Serverless → New Endpoint → **GitHub Repo** source
(not Docker Hub/GHCR — auto-build-on-push per spec).

1. Connect the GitHub repo, set build context to `worker-video/` so it
   picks up that directory's `Dockerfile` (repeat this whole section
   with `worker-image/` when you add the image endpoint)
2. GPU: **RTX 5090**
3. Workers: min **0**, max **10–20** for video (image: **5–10**,
   lighter load expected — adjust after real traffic data)
4. Idle timeout: **5s**
5. Attach the Network Volume created in step 1 for this worker —
   mounts at `/runpod-volume`, which is what each `handler.py` expects
6. Leave Bearer auth on (RunPod's default — do not disable)
7. Deploy, wait for the first build to finish

## 3. Capture the endpoint URL + key

Each endpoint gets its own Bearer key and its own runsync URL, shaped
like `https://api.runpod.ai/v2/<endpoint-id>/runsync`.

Map the video values into `backend/.env` now (see
`backend/.env.example`); leave `RUNPOD_IMAGE_*` unset until that
endpoint exists — `POST /generate-image` just won't work until then.

| RunPod value | Env var |
|---|---|
| Video endpoint URL | `RUNPOD_VIDEO_ENDPOINT` |
| Video endpoint Bearer key | `RUNPOD_VIDEO_KEY` |
| Image endpoint URL | `RUNPOD_IMAGE_ENDPOINT` |
| Image endpoint Bearer key | `RUNPOD_IMAGE_KEY` |

Keep the two keys distinct secrets — compromise of one must not expose
the other (`backend/app/config.py`'s `all_secrets()` already redacts
both from logs, but only if each is actually a separate value here).

## 4. Verify auth is enforced

Before wiring the backend, confirm unauthenticated calls are rejected:

```bash
curl -X POST https://api.runpod.ai/v2/<endpoint-id>/runsync \
  -H "Content-Type: application/json" \
  -d '{"input": {}}'
# expect 401/403, NOT a handler response
```

Then confirm the real key works:

```bash
curl -X POST https://api.runpod.ai/v2/<endpoint-id>/runsync \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "test", "duration": 5}}'
# video worker will currently raise NotImplementedError inside
# _generate_video — that's expected until real inference is wired in
```

Same call shape for image, with `worker-image`'s expected input fields
instead (check `worker-image/handler.py`'s `validate_input`).

## 5. Wire into the backend

Fill `backend/.env` from `backend/.env.example` with the four RunPod
values from step 3, plus the rest of the required settings
(`backend/app/config.py`'s `Settings`). Then:

```bash
cd backend && python -m pytest tests/test_runpod_client.py -v
```

This only exercises the dispatch client against mocks — it doesn't hit
the real endpoints. To confirm the real endpoints from the backend
side, run `docker compose up --build` locally and hit
`POST /generate-image` / `POST /generate-video` with valid RunPod env
vars set, watching for the `NotImplementedError` surfacing back through
as a job failure (expected — real inference isn't vendored yet).

## Known gap

`_generate_video` (`worker-video/handler.py`) and `_generate_image`
(`worker-image/handler.py`) both raise `NotImplementedError`. Wiring
real LTX-2.3 / FLUX.1-schnell inference calls is a separate task and
needs the actual model repos — this guide only gets the endpoints
deployed and reachable, not producing real output.
