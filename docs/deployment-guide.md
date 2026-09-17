# Deployment Guide (First-Time, Start to Finish) — Railway

This walks through deploying Lantaw Generator from nothing to a working
service on Railway, assuming you've never done this before. Follow it
top to bottom in order — later steps depend on values from earlier ones.

**Scope of this pass:** video generation only. The image endpoint
(FLUX.1-schnell) is skipped for now per `docs/runpod-setup-guide.md` —
you can add it later without redoing any of this.

**No custom domain needed.** Railway gives every service a free
generated HTTPS subdomain (e.g.
`lantaw-generator-production.up.railway.app`) with TLS already handled
— that's enough for everything in this guide. Buying a domain is
optional and purely cosmetic; skip it unless you want your own branded
URL later.

**What you're building:**

```
You (browser/curl)
   │  HTTPS (Railway-managed TLS, generated domain)
   ▼
FastAPI backend (auth, rate limit, moderation, job queue)  ─┐
   │              │                                          │  Railway
   ▼              ▼                                          │  project
Postgres      Redis + RQ worker ──► RunPod video endpoint    │  (3-4
(job records)  (job queue)          (does the actual GPU work)│  services)
                     │                                        │
                     ▼                                        │
              Cloudflare R2 (stores results) ◄─────────────────┘
```

Backend, RQ worker, Postgres, and Redis all run as separate services
inside **one Railway project** — no VM, no Docker Compose, no Caddy.
Railway builds each service straight from this repo's Dockerfiles.
RunPod and Cloudflare R2 stay external managed services configured
through their own web consoles, same as before.

---

## Part 0: Accounts you need

1. **Railway account** — https://railway.app — sign up (GitHub login
   is easiest since it can read your repo directly).
2. **RunPod account** — https://runpod.io — for the GPU worker.
3. **Cloudflare account** — for R2 object storage (S3-compatible,
   cheaper egress than S3).
4. **GitHub account** — you already have the code pushed to
   `https://github.com/imneil24/lantaw-generator.git`.

No VPS provider, no domain registrar needed for this pass.

---

## Part 1: Create the Railway project

1. Railway dashboard → New Project → **Deploy from GitHub repo** →
   select `imneil24/lantaw-generator`.
2. Railway will try to auto-detect a service from the repo root — this
   repo has **no root Dockerfile** (each component has its own under
   `backend/`, `worker-video/`, `worker-image/`), so cancel/skip that
   first auto-created service; you'll add services manually in the
   next steps pointing at the right subdirectory each time.
3. In the project, add the two managed data stores first:
   - **+ New → Database → PostgreSQL** — Railway provisions it and
     exposes `DATABASE_URL` automatically as a reference variable
     (`${{Postgres.DATABASE_URL}}`)
   - **+ New → Database → Redis** — exposes
     `${{Redis.REDIS_URL}}` the same way

---

## Part 2: Add the backend service

1. **+ New → GitHub Repo** → same repo again → this creates a second
   service. Rename it to `backend`.
2. Service → Settings → **Root Directory**: set to `backend` — this is
   the equivalent of `docker-compose.yml`'s `build: ./backend`; Railway
   will build `backend/Dockerfile` using only that subfolder as context.
3. Settings → Networking → **Generate Domain** — this gives you the
   public HTTPS URL (`https://<something>.up.railway.app`) that
   replaces `BACKEND_DOMAIN`/Caddy entirely. Note the exact URL.
4. Settings → Networking → confirm the exposed port is **8000**
   (matches `EXPOSE 8000` / the `uvicorn --port 8000` command in
   `backend/Dockerfile`) — Railway usually detects this from the
   `EXPOSE` line, but check it explicitly.
5. Settings → **Watch Paths** → set to `backend/**`. Without this,
   Railway redeploys on every push to `master` regardless of which
   directory changed — a `worker-video/`-only fix would needlessly
   restart the live backend and briefly interrupt request handling.

Leave the environment variables for now — set them all together in
Part 6.

---

## Part 3: Add the RQ worker service

The worker processes queued jobs (dispatches to RunPod, stitches clips)
— it's the same codebase as the backend but a different start command,
exactly like `docker-compose.yml`'s separate `rq_worker` service.

1. **+ New → GitHub Repo** → same repo again → third service, rename
   to `rq-worker`.
2. Settings → **Root Directory**: `backend` (same code, same
   Dockerfile as the backend service).
3. Settings → Deploy → **Custom Start Command**, override the
   Dockerfile's default `CMD` with:
   ```
   rq worker clip-image-jobs --url $REDIS_URL
   ```
   The queue name `clip-image-jobs` **must match exactly** — it's
   hardcoded as `QUEUE_NAME` in `backend/app/main.py`. A typo here
   means jobs get created but nothing ever processes them.
4. This service does **not** need Generate Domain / a public port — it
   only consumes from Redis, nothing calls it over HTTP.
5. Settings → **Watch Paths** → set to `backend/**`, same reasoning as
   the backend service in Part 2.

---

## Part 4: Set up Cloudflare R2 (object storage)

Finished video clips get uploaded here; the backend gives callers a
signed URL to download them.

1. Cloudflare dashboard → R2 → Create bucket. Name it something like
   `lantaw-media`.
2. R2 → Manage API Tokens → Create API Token. Create **two** tokens
   (matches the two-key split in `backend/app/config.py` — a
   write-compromise shouldn't also grant read/delete elsewhere):
   - One with **Object Read & Write** permission, scoped to this bucket
     → gives you `R2_WRITE_KEY` / `R2_WRITE_SECRET`
   - One with **Object Read only** permission, scoped to this bucket →
     gives you `R2_READ_KEY` / `R2_READ_SECRET`
3. Note your R2 **endpoint URL** — shown on the bucket's or account's
   R2 overview page, shaped like
   `https://<account-id>.r2.cloudflarestorage.com`.
4. **Set up the lifecycle rule now, don't skip it** — otherwise
   finished videos accumulate in the bucket forever and cost creeps up.
   Bucket → Settings → Object lifecycle rules → add a rule: expire
   objects under the `videos/` prefix 1 hour after upload (matches the
   signed URL's 1-hour expiry). Leave the `clips/` prefix alone — the
   stitcher may need to re-read raw clips on retry. Full detail in the
   README's "R2 bucket lifecycle" section.

You should now have: `R2_WRITE_KEY`, `R2_WRITE_SECRET`, `R2_READ_KEY`,
`R2_READ_SECRET`, `R2_BUCKET` (the bucket name), `R2_ENDPOINT`.

---

## Part 5: Set up the RunPod video endpoint

Full detail already written up in `docs/runpod-setup-guide.md` — follow
that document now for:

1. Creating a Network Volume and uploading LTX-2.3 model weights to it
2. Creating the Serverless endpoint from the `worker-video/` GitHub
   path, GPU RTX 5090, min 0 / max 10–20 workers, 5s idle timeout
3. Verifying the endpoint rejects unauthenticated calls
4. Capturing `RUNPOD_VIDEO_KEY` and `RUNPOD_VIDEO_ENDPOINT`

Come back here once you have those two values. Leave
`RUNPOD_IMAGE_KEY` / `RUNPOD_IMAGE_ENDPOINT` as placeholders for now —
the image endpoint isn't part of this pass.

**Important limitation to set expectations correctly:** the actual
LTX-2.3 inference call inside `worker-video/handler.py` is not wired up
in this codebase yet — it raises `NotImplementedError` by design (see
CLAUDE.md / the plan doc). This guide gets you a fully working
*pipeline* (auth → queue → dispatch → stitch → storage), but the
worker itself won't produce a real video until that integration is
completed separately. Don't be alarmed when a submitted video job ends
in a failure with that error — that's the expected current state, not
something this deploy guide got wrong.

---

## Part 6: Generate the remaining secrets

You'll need a machine with Python available (your own laptop is fine —
these are one-time local commands, nothing here runs on Railway).

**Backend caller API key** — this is the Bearer token *your own
clients* will use to call the backend. Pick a random key, then hash it
with bcrypt (never store the plaintext key — `backend_api_key_hash` in
`backend/app/config.py` expects a bcrypt hash, compared via
`hmac.compare_digest`):
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# ^ this is your plaintext API key — save it somewhere safe, you'll
#   need to send it as "Authorization: Bearer <this>" on every request

python3 -m pip install bcrypt --quiet
python3 -c "import bcrypt; print(bcrypt.hashpw(b'PASTE-THE-KEY-ABOVE-HERE', bcrypt.gensalt()).decode())"
# ^ this is what goes into BACKEND_API_KEY_HASH
```

**Webhook URL** — used for cost/scale-drift/revenue alerts from the
`ops/` scripts (not required for the core service to run, but the
`Settings` schema requires *some* value). A Slack incoming webhook URL
works fine; if you don't have one yet, any placeholder HTTPS URL lets
the app start, but alerts silently won't be delivered until it's real.

**Moderation API key** — optional (`moderation_api_key: str | None` in
config). Leave blank unless you're wiring in a third-party moderation
provider.

**Postgres/Redis credentials** — you don't generate these yourself;
Railway already created them in Part 1 and exposes them as reference
variables, used directly in Part 7.

---

## Part 7: Set environment variables on Railway

Both the `backend` and `rq-worker` services need the **same** env vars
(they run identical code, just different start commands) — configure
each service's Variables tab with:

```
RUNPOD_VIDEO_KEY=<from Part 5>
RUNPOD_IMAGE_KEY=changeme
RUNPOD_VIDEO_ENDPOINT=<from Part 5, e.g. https://api.runpod.ai/v2/xxxxx/runsync>
RUNPOD_IMAGE_ENDPOINT=https://api.runpod.ai/v2/CHANGEME/runsync
R2_WRITE_KEY=<from Part 4>
R2_WRITE_SECRET=<from Part 4>
R2_READ_KEY=<from Part 4>
R2_READ_SECRET=<from Part 4>
R2_BUCKET=<from Part 4>
R2_ENDPOINT=<from Part 4>
POSTGRES_DSN=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
BACKEND_API_KEY_HASH=<bcrypt hash from Part 6>
WEBHOOK_URL=<from Part 6>
MODERATION_API_KEY=
```

`${{Postgres.DATABASE_URL}}` and `${{Redis.REDIS_URL}}` are Railway
**reference variables** — type them literally like that in the
Variables tab (not the real connection string); Railway resolves them
to the actual live values from the database services you created in
Part 1. This is the same pattern Lantaw's own `railway.json` uses.

One catch: `backend/app/config.py`'s `Settings` reads `postgres_dsn`,
but Railway's Postgres add-on exposes a Postgres URL starting with
`postgresql://` — that's already the correct scheme SQLAlchemy expects
here, no rewriting needed.

Fastest way to apply the same variables to both services: set them
once on `backend`, then use Railway's "Copy variables from another
service" (or just paste the same block) into `rq-worker`.

---

## Part 8: Deploy

Railway auto-deploys on push once a service is connected to a repo
branch — pushing to `master` (or whatever branch you connected)
triggers a build for `backend` and `rq-worker` using their configured
Root Directory.

If nothing has changed since you connected the repo, trigger the first
build manually: each service → Deployments tab → **Deploy**.

Watch the build+deploy logs for each service:

- `backend` — look for uvicorn startup with no traceback, and no
  `pydantic.ValidationError` (that error means a required env var from
  Part 7 is missing/misnamed — `Settings` is a required-fields model)
- `rq-worker` — look for `Listening on clip-image-jobs...`
- **Postgres/Redis** — these don't need "deploying", just confirm
  they show as Active in the project view

---

## Part 9: Verify it's alive

Use the domain you generated in Part 2 (Settings → Networking →
Generate Domain), e.g. `https://lantaw-generator-production.up.railway.app`.

```bash
curl -i https://<your-app>.up.railway.app/jobs/00000000-0000-0000-0000-000000000000 \
  -H "Authorization: Bearer <your plaintext backend API key from Part 6>"
```

Expect a `404` (job doesn't exist) rather than a connection error or
`401` — a 404 means auth passed and the app is routing requests
correctly. A `401` means the Bearer key doesn't match the hash in
`BACKEND_API_KEY_HASH`; recheck Part 6. A connection failure/502 means
the backend service isn't running; check its Railway logs.

Submit a real video job:

```bash
curl -X POST https://<your-app>.up.railway.app/generate-video \
  -H "Authorization: Bearer <your API key>" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a calm ocean at sunset", "target_duration": 10}'
```

This should return a project id right away (job creation, not
completion — video generation is async). Poll it:

```bash
curl https://<your-app>.up.railway.app/projects/<project-id-from-above> \
  -H "Authorization: Bearer <your API key>"
```

**Expected outcome right now:** the job will end in a failed state
because `worker-video/handler.py`'s `_generate_video` raises
`NotImplementedError` (see Part 5's callout — real inference isn't
wired in yet). Reaching that failure state — job created, picked up by
`rq-worker`, dispatched to RunPod, and RunPod's response coming back
through — **is** the successful outcome of this deploy. It confirms
every piece of your infrastructure is correctly wired end to end; only
the model inference call itself remains as separate work.

**Note on rate limiting:** the backend enforces 10 requests/60 seconds
per caller (`SlidingWindowLimiter` in `backend/app/main.py`). If you're
testing repeatedly and start getting `429`s, that's this limiter, not
a bug.

---

## Part 10: Common problems

| Symptom | Likely cause |
|---|---|
| `backend`/`rq-worker` service crashes on boot | Missing/malformed env var in Part 7 — `Settings` is a required-fields Pydantic model, a missing var crashes startup. Check the service's Deploy Logs |
| `rq-worker` never picks up jobs | Queue name mismatch — must be exactly `clip-image-jobs` in the custom start command (Part 3) and match `QUEUE_NAME` in `backend/app/main.py` |
| Railway build fails, "Dockerfile not found" | Root Directory not set to `backend` for that service (Part 2/3) |
| `401 Unauthorized` on every request | Bearer key doesn't match `BACKEND_API_KEY_HASH`, or you hashed the wrong string |
| `502 Bad Gateway` from the generated domain | Backend not listening on the port Railway expects — confirm Networking settings match the Dockerfile's `EXPOSE 8000` |
| RunPod dispatch fails (`RuntimeError: RunPod video dispatch failed`) | Wrong `RUNPOD_VIDEO_ENDPOINT`/`RUNPOD_VIDEO_KEY`, or the endpoint isn't deployed yet — recheck `docs/runpod-setup-guide.md` Part 2's curl verification steps |
| `429 Too Many Requests` while testing | Rate limiter (10 req/60s) — wait and retry, not a misconfiguration |

---

## Part 11: What's NOT covered here

- **Real video inference** — `_generate_video` in `worker-video/handler.py`
  is a stub (`NotImplementedError`). Wiring the actual LTX-2.3 model
  call is separate follow-up work needing the real model repo/weights.
- **Image generation endpoint** — skipped this pass; repeat Part 5's
  RunPod steps for `worker-image/` and add a corresponding
  `image-rq-worker` or reuse `rq-worker` (it already dispatches both
  `process_clip_job` and `process_image_job` off the same queue) once
  you fill in `RUNPOD_IMAGE_*`.
- **Real TTS audio** — the stitcher currently mixes in silent audio
  (`NullTTSProvider`); no real text-to-speech is wired in.
- **Custom domain** — optional; if you want one later, Railway service
  → Settings → Networking → Custom Domain, then add the CNAME Railway
  gives you at your registrar.
- **Backups, staging environments, horizontal scaling, alerting
  dashboards** — this guide gets you a working deployment, not a
  production-hardened one.
