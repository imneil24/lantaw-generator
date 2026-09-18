import logging
import uuid
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
import redis as redis_lib
from rq import Queue, Retry

from app.config import get_settings
from app.logging_conf import configure_logging
from app.db import get_engine, session_factory
from app.models import Base
from app.moderation import KeywordModerationProvider
from app.r2 import R2Client
from app.middleware.auth import make_auth_dependency
from app.middleware.rate_limit import SlidingWindowLimiter, make_rate_limit_dependency
from app.routes import generate_image, generate_video, jobs
from app import webhooks
from app.queue import process_clip_job, process_image_job, MAX_RETRIES

logger = logging.getLogger(__name__)

QUEUE_NAME = "clip-image-jobs"  # must match the `rq worker` invocation in docker-compose.yml


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.all_secrets())

    engine = get_engine(settings.postgres_dsn)
    Base.metadata.create_all(engine)
    Session = session_factory(engine)

    moderation = KeywordModerationProvider()
    r2_client = R2Client(settings)
    redis_client = redis_lib.from_url(settings.redis_url)
    limiter = SlidingWindowLimiter(redis_client, max_requests=10, window_seconds=60)
    job_queue = Queue(QUEUE_NAME, connection=redis_client)

    auth_dependency = make_auth_dependency(settings.backend_api_key_hash)
    rate_limit_dependency = make_rate_limit_dependency(limiter)

    # Applied at app construction, not per-router: every route in this
    # service requires auth, so the default is protected-unless-excluded
    # rather than protected-only-if-a-router-remembers-to-opt-in. A route
    # registered later via app.include_router(new_router) without passing
    # its own `dependencies=` still inherits these — there is no way to add
    # an unauthenticated route by omission, only by explicitly excluding one
    # (which this app never needs to do).
    app = FastAPI(dependencies=[Depends(auth_dependency), Depends(rate_limit_dependency)])

    def db_session_override():
        return Session()

    def moderation_override():
        return moderation

    def r2_override():
        return r2_client

    def make_enqueue(job_type: str):
        target = process_clip_job if job_type == "clip" else process_image_job

        def enqueue(job_id: str):
            # Only job_id crosses the process boundary — process_clip_job/
            # process_image_job rebuild their own DB session and API clients
            # from env-derived Settings rather than receiving live,
            # unpicklable connections as arguments.
            # process_clip_job/process_image_job now dispatch-and-return
            # (a single RunPod /run POST + DB commit) rather than blocking
            # for the full generation duration — see webhooks.py for how
            # completion is now driven by RunPod's callback instead of an
            # in-job poll loop. 120s is generous headroom over the
            # dispatch call's own 30s httpx timeout.
            job_queue.enqueue(target, job_id, retry=Retry(max=MAX_RETRIES), job_timeout=120)
        return enqueue

    app.include_router(generate_image.router)
    app.include_router(generate_video.router)
    app.include_router(jobs.router)

    app.dependency_overrides[generate_image.get_db_session] = db_session_override
    app.dependency_overrides[generate_image.get_moderation] = moderation_override
    app.dependency_overrides[generate_image.get_queue_enqueue] = lambda: make_enqueue("image")

    app.dependency_overrides[generate_video.get_db_session] = db_session_override
    app.dependency_overrides[generate_video.get_moderation] = moderation_override
    app.dependency_overrides[generate_video.get_queue_enqueue] = lambda: make_enqueue("clip")

    app.dependency_overrides[jobs.get_db_session] = db_session_override
    app.dependency_overrides[jobs.get_r2_client] = r2_override

    # Mounted as a separate FastAPI sub-application, not include_router'd
    # onto `app` — dependencies passed to FastAPI(...)'s constructor apply
    # to every route on that instance app-wide, including routers added
    # later via include_router, with no per-router opt-out (see
    # test_route_added_without_explicit_dependencies_still_requires_auth in
    # test_main.py, which asserts exactly this). RunPod's webhook callback
    # carries no bearer key matching backend_api_key_hash, so this router
    # must live on an instance that never had auth_dependency/
    # rate_limit_dependency applied to it at all — a mount is the only way
    # to achieve that. It is gated solely by the path secret (webhooks.py).
    webhook_app = FastAPI()
    webhook_app.include_router(webhooks.router)
    webhook_app.dependency_overrides[webhooks.get_db_session] = db_session_override
    webhook_app.dependency_overrides[webhooks.get_r2_client] = r2_override
    webhook_app.dependency_overrides[webhooks.get_webhook_secret] = lambda: settings.runpod_webhook_secret
    app.mount("/webhooks", webhook_app)

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        correlation_id = str(uuid.uuid4())
        logger.error("unhandled exception [correlation_id=%s]: %s", correlation_id, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error", "correlation_id": correlation_id},
        )

    return app
