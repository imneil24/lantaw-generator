import base64
import logging
import os
import tempfile
import uuid
from functools import lru_cache
from sqlalchemy.orm.exc import NoResultFound
from rq import get_current_job

from app.config import get_settings
from app.db import get_engine, session_factory
from app.models import Job, ProjectClip, VideoProject
from app.r2 import R2Client
from app.runpod_client import RunpodClient
from app.stitcher import stitch_project
from app.tts import NullTTSProvider

logger = logging.getLogger(__name__)

MAX_RETRIES = 3  # passed to RQ's Retry(max=...) in main.py's enqueue() call


@lru_cache
def _get_worker_clients():
    """Builds the RunPod/R2 clients once per worker process, not per job.

    RQ pickles job arguments into Redis for a separate worker process to
    deserialize, so job functions can't receive live clients as arguments
    (see main.py's enqueue). But within one already-running worker process,
    rebuilding a fresh Engine/connection-pool and API client on every single
    job call is pure waste — this caches them for the life of the process.
    """
    settings = get_settings()
    engine = get_engine(settings.postgres_dsn)
    Session = session_factory(engine)
    return Session, RunpodClient(settings), R2Client(settings)


def _build_dependencies():
    Session, runpod_client, r2_client = _get_worker_clients()
    return Session(), runpod_client, r2_client


def _is_final_attempt() -> bool:
    """True if RQ has no retries left for the currently-executing job.

    RQ's own Retry(max=...) (see main.py's enqueue call) is the single
    source of truth for how many attempts a job gets — job.retry_count in
    the DB only mirrors that decision for observability, it does not gate
    anything itself. Two independent counters deciding the same thing can
    drift if a crash lands between the RQ and DB writes; reading RQ's own
    state here avoids that split-brain.
    """
    current = get_current_job()
    if current is None or current.retries_left is None:
        return True
    return current.retries_left <= 0


def _handle_failure(session, job: Job, exc: Exception) -> None:
    # Always re-raise: RQ's own Retry(max=...) (configured in main.py's
    # enqueue call) decides whether to re-enqueue or move the job to its
    # FailedJobRegistry based on RQ's internal retries_left, not on
    # anything this function returns. Suppressing the exception on what we
    # think is the final attempt would make RQ believe the job succeeded,
    # hiding the failure from RQ's own monitoring/registries.
    job.retry_count += 1
    if _is_final_attempt():
        job.status = "failed"
    session.commit()
    raise exc


def _maybe_stitch_project(session, r2_client, job: Job) -> None:
    """After a clip job completes, stitch its project once every sibling clip is done.

    Runs in its own try/except, separate from the caller's clip-dispatch
    handling: a download/ffmpeg failure here must not be treated as a
    failure of the clip job that already succeeded and was already billed
    for — that would re-dispatch RunPod pointlessly and could eventually
    flip an already-successful clip's status to "failed".
    """
    try:
        own_clip = session.query(ProjectClip).filter_by(job_id=job.id).one_or_none()
        if own_clip is None:
            return  # this job isn't part of a video project (e.g. a standalone image job)

        rows = (
            session.query(ProjectClip, Job)
            .join(Job, Job.id == ProjectClip.job_id)
            .filter(ProjectClip.project_id == own_clip.project_id)
            .order_by(ProjectClip.sequence_index)
            .all()
        )
        project = session.query(VideoProject).filter_by(id=own_clip.project_id).one()
        if project.status == "complete":
            return  # already stitched by a prior (possibly redelivered) run
        if any(clip_job.status != "complete" for _, clip_job in rows):
            return

        with tempfile.TemporaryDirectory() as tmp_dir:
            clip_paths = []
            for clip, clip_job in rows:
                clip_bytes = r2_client.download(clip_job.result_key)
                clip_path = os.path.join(tmp_dir, f"clip_{clip.sequence_index}.mp4")
                with open(clip_path, "wb") as f:
                    f.write(clip_bytes)
                clip_paths.append(clip_path)

            audio_path = NullTTSProvider(output_dir=tmp_dir).generate(
                text="", target_duration=project.target_duration,
            )
            output_path = os.path.join(tmp_dir, "final.mp4")
            stitch_project(clip_paths, audio_path, output_path)

            with open(output_path, "rb") as f:
                final_bytes = f.read()
            final_key = f"videos/{uuid.uuid4().hex}.mp4"
            r2_client.upload(final_key, final_bytes, "video/mp4")

        project.status = "complete"
        project.final_result_key = final_key
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("stitching failed for job_id=%s; clip job is unaffected", job.id)


def _extract_output(result: dict) -> dict:
    # RunPod's handler returns {"error": "..."} (not {"output": ...}) for
    # validation/moderation rejections and unimplemented-inference stubs —
    # surface that as a clear RuntimeError instead of a raw KeyError so
    # _handle_failure's retry/failure bookkeeping gets a legible message.
    if "output" not in result:
        raise RuntimeError(f"RunPod job returned no output: {result.get('error', result)}")
    output = result["output"]
    # RunPod has been observed marking a job COMPLETED with an empty/None
    # output when the worker container is killed outright (e.g. an OOM
    # kill) rather than the handler raising a catchable exception — that
    # case must fail loudly here instead of crashing downstream on
    # output["key"] with an unrelated-looking KeyError.
    if not output:
        raise RuntimeError("RunPod job completed but returned an empty output (worker likely crashed)")
    return output


def process_clip_job(job_id: str) -> None:
    session, runpod_client, r2_client = _build_dependencies()
    try:
        try:
            job = session.query(Job).filter_by(id=job_id).one()
        except NoResultFound:
            return

        if job.status == "complete":
            return  # redelivered/duplicate job; already processed

        def _persist_runpod_job_id(runpod_job_id: str) -> None:
            job.runpod_job_id = runpod_job_id
            session.commit()

        try:
            result = runpod_client.dispatch_video(
                prompt=job.prompt, duration=job.duration, on_submitted=_persist_runpod_job_id,
            )
            output = _extract_output(result)
            # The worker uploads the clip to R2 itself and returns only the
            # key (see worker-video/handler.py) — a full HD video
            # base64-encoded into the job result is too large for RunPod's
            # own /job-done callback, which rejects it with a 400 before the
            # backend ever sees a completed job.
            job.status = "complete"
            job.result_key = output["key"]
            session.commit()
        except Exception as exc:
            _handle_failure(session, job, exc)
            return

        _maybe_stitch_project(session, r2_client, job)
    finally:
        session.close()


def process_image_job(job_id: str) -> None:
    session, runpod_client, r2_client = _build_dependencies()
    try:
        try:
            job = session.query(Job).filter_by(id=job_id).one()
        except NoResultFound:
            return

        if job.status == "complete":
            return  # redelivered/duplicate job; already processed

        def _persist_runpod_job_id(runpod_job_id: str) -> None:
            job.runpod_job_id = runpod_job_id
            session.commit()

        try:
            result = runpod_client.dispatch_image(prompt=job.prompt, on_submitted=_persist_runpod_job_id)
            output = _extract_output(result)
            raw_bytes = base64.b64decode(output["bytes_b64"])
            key = output["key"]
            r2_client.upload(key, raw_bytes, "image/png")
            job.status = "complete"
            job.result_key = key
            session.commit()
        except Exception as exc:
            _handle_failure(session, job, exc)
    finally:
        session.close()
