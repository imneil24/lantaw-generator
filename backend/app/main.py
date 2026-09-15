import logging
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import redis as redis_lib
from rq import Queue

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
    job_queue = Queue("clip-image-jobs", connection=redis_client)

    auth_dependency = make_auth_dependency(settings.backend_api_key_hash)

    def db_session_override():
        return Session()

    def moderation_override():
        return moderation

    def r2_override():
        return r2_client

    def make_enqueue(job_type: str):
        target = process_clip_job if job_type == "clip" else process_image_job

        def enqueue(job_id: str):
            job_queue.enqueue(target, job_id, Session, runpod_client, r2_client)
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

    protected_prefixes = ("/generate-image", "/generate-video", "/jobs/", "/projects/")

    @app.middleware("http")
    async def enforce_auth_and_rate_limit(request: Request, call_next):
        if request.url.path.startswith(protected_prefixes):
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
