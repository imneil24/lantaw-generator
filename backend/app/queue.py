import base64
import os
import tempfile
import uuid
from sqlalchemy.orm.exc import NoResultFound

from app.config import get_settings
from app.db import get_engine, session_factory
from app.models import Job, ProjectClip, VideoProject
from app.r2 import R2Client
from app.runpod_client import RunpodClient
from app.stitcher import stitch_project
from app.tts import NullTTSProvider

MAX_RETRIES = 3


def _build_dependencies():
    """Constructs fresh DB session + API clients from env-derived Settings.

    RQ pickles job arguments into Redis for a separate worker process to
    deserialize. A live SQLAlchemy sessionmaker or boto3/httpx client wraps
    open sockets and connection-pool internals that don't survive that trip
    intact, so job functions rebuild everything here from Settings instead
    of receiving live objects as arguments.
    """
    settings = get_settings()
    engine = get_engine(settings.postgres_dsn)
    Session = session_factory(engine)
    return Session(), RunpodClient(settings), R2Client(settings)


def _handle_failure(session, job: Job, exc: Exception) -> None:
    if job.retry_count >= MAX_RETRIES:
        job.status = "failed"
        session.commit()
        return
    job.retry_count += 1
    session.commit()
    raise exc  # let RQ's Retry mechanism re-enqueue the job


def _maybe_stitch_project(session, r2_client, job: Job) -> None:
    """After a clip job completes, stitch its project once every sibling clip is done."""
    clip = session.query(ProjectClip).filter_by(job_id=job.id).one_or_none()
    if clip is None:
        return

    project = session.query(VideoProject).filter_by(id=clip.project_id).one()
    sibling_clips = (
        session.query(ProjectClip)
        .filter_by(project_id=project.id)
        .order_by(ProjectClip.sequence_index)
        .all()
    )
    sibling_jobs = {
        j.id: j for j in session.query(Job).filter(Job.id.in_([c.job_id for c in sibling_clips])).all()
    }
    if any(sibling_jobs[c.job_id].status != "complete" for c in sibling_clips):
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        clip_paths = []
        for c in sibling_clips:
            clip_job = sibling_jobs[c.job_id]
            clip_bytes = r2_client.download(clip_job.result_key)
            clip_path = os.path.join(tmp_dir, f"clip_{c.sequence_index}.mp4")
            with open(clip_path, "wb") as f:
                f.write(clip_bytes)
            clip_paths.append(clip_path)

        audio_path = NullTTSProvider(output_dir=tmp_dir).generate(text="", target_duration=project.target_duration)
        output_path = os.path.join(tmp_dir, "final.mp4")
        stitch_project(clip_paths, audio_path, output_path)

        with open(output_path, "rb") as f:
            final_bytes = f.read()
        final_key = f"videos/{uuid.uuid4().hex}.mp4"
        r2_client.upload(final_key, final_bytes, "video/mp4")

    project.status = "complete"
    project.final_result_key = final_key
    session.commit()


def process_clip_job(job_id: str) -> None:
    session, runpod_client, r2_client = _build_dependencies()
    try:
        try:
            job = session.query(Job).filter_by(id=job_id).one()
        except NoResultFound:
            return

        try:
            result = runpod_client.dispatch_video(prompt=job.prompt, duration=job.duration)
            raw_bytes = base64.b64decode(result["output"]["bytes_b64"])
            key = result["output"]["key"]
            r2_client.upload(key, raw_bytes, "video/mp4")
            job.status = "complete"
            job.result_key = key
            session.commit()
            _maybe_stitch_project(session, r2_client, job)
        except Exception as exc:
            _handle_failure(session, job, exc)
    finally:
        session.close()


def process_image_job(job_id: str) -> None:
    session, runpod_client, r2_client = _build_dependencies()
    try:
        try:
            job = session.query(Job).filter_by(id=job_id).one()
        except NoResultFound:
            return

        try:
            result = runpod_client.dispatch_image(prompt=job.prompt)
            raw_bytes = base64.b64decode(result["output"]["bytes_b64"])
            key = result["output"]["key"]
            r2_client.upload(key, raw_bytes, "image/png")
            job.status = "complete"
            job.result_key = key
            session.commit()
        except Exception as exc:
            _handle_failure(session, job, exc)
    finally:
        session.close()
