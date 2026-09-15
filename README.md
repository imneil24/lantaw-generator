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

## Running tests

Each component has its own virtualenv and test suite:

```bash
cd backend && python -m pytest -v
cd worker-video && python -m pytest -v
cd worker-image && python -m pytest -v
cd ops && python -m pytest -v
```

## Known integration gaps (deliberately out of TDD scope)

- `worker-video/handler.py` `_generate_video` and `worker-image/handler.py`
  `_generate_image` raise `NotImplementedError` — wiring the real LTX-2.3 and
  FLUX.1-schnell inference calls requires the actual model repos/weights and
  a GPU, so it isn't unit-testable in this environment.
- RunPod Serverless endpoint creation (GPU type, min/max workers, Network
  Volume attachment) is done via the RunPod console/API directly, not code.
- `docker-compose.yml`'s Caddy TLS sidecar requires a real domain pointed at
  the host to issue a certificate — verify after deployment.
