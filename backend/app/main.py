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
from app.queue import process_clip_job, process_image_job, MAX_RETRIES

logger = logging.getLogger(__name__)

QUEUE_NAME = "clip-image-jobs"  # must match the `rq worker` invocation in docker-compose.yml


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.all_secrets())

    app = FastAPI()

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
            job_queue.enqueue(target, job_id, retry=Retry(max=MAX_RETRIES))
        return enqueue

    # Auth runs before rate-limiting in this list — FastAPI resolves
    # dependencies in declared order, so an invalid/missing key gets
    # rejected without ever touching Redis.
    protected_dependencies = [Depends(auth_dependency), Depends(rate_limit_dependency)]
    app.include_router(generate_image.router, dependencies=protected_dependencies)
    app.include_router(generate_video.router, dependencies=protected_dependencies)
    app.include_router(jobs.router, dependencies=protected_dependencies)

    app.dependency_overrides[generate_image.get_db_session] = db_session_override
    app.dependency_overrides[generate_image.get_moderation] = moderation_override
    app.dependency_overrides[generate_image.get_queue_enqueue] = lambda: make_enqueue("image")

    app.dependency_overrides[generate_video.get_db_session] = db_session_override
    app.dependency_overrides[generate_video.get_moderation] = moderation_override
    app.dependency_overrides[generate_video.get_queue_enqueue] = lambda: make_enqueue("clip")

    app.dependency_overrides[jobs.get_db_session] = db_session_override
    app.dependency_overrides[jobs.get_r2_client] = r2_override

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        correlation_id = str(uuid.uuid4())
        logger.error("unhandled exception [correlation_id=%s]: %s", correlation_id, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error", "correlation_id": correlation_id},
        )

    return app
