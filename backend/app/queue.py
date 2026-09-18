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


def _finish_clip_job(session, r2_client, job: Job, result: dict) -> None:
    output = _extract_output(result)
    # The worker uploads the clip to R2 itself and returns only the key (see
    # worker-video/handler.py) — a full HD video base64-encoded into the job
    # result is too large for RunPod's own /job-done callback, which rejects
    # it with a 400 before the backend ever sees a completed job.
    job.status = "complete"
    job.result_key = output["key"]
    session.commit()
    _maybe_stitch_project(session, r2_client, job)


def _finish_image_job(session, r2_client, job: Job, result: dict) -> None:
    output = _extract_output(result)
    raw_bytes = base64.b64decode(output["bytes_b64"])
    key = output["key"]
    r2_client.upload(key, raw_bytes, "image/png")
    job.status = "complete"
    job.result_key = key
    session.commit()


def process_clip_job(job_id: str) -> None:
    session, runpod_client, r2_client = _build_dependencies()
    try:
        try:
            job = session.query(Job).filter_by(id=job_id).one()
        except NoResultFound:
            return

        if job.status == "complete":
            return  # redelivered/duplicate job; already processed

        try:
            if job.runpod_job_id is not None:
                # A prior attempt already submitted this to RunPod (its
                # runpod_job_id was persisted) but never got the result back
                # — e.g. this same worker process's resume_orphaned_jobs
                # sweep is racing an RQ redelivery of this job_id. Poll the
                # existing RunPod job instead of dispatching a second,
                # duplicate one for the same prompt.
                result = runpod_client.poll_video(job.runpod_job_id)
            else:
                def _persist_runpod_job_id(runpod_job_id: str) -> None:
                    job.runpod_job_id = runpod_job_id
                    session.commit()

                result = runpod_client.dispatch_video(
                    prompt=job.prompt, duration=job.duration, on_submitted=_persist_runpod_job_id,
                )
        except Exception as exc:
            _handle_failure(session, job, exc)
            return

        _finish_clip_job(session, r2_client, job, result)
    finally:
        session.close()


def resume_orphaned_jobs() -> None:
    """Resumes clip jobs that were dispatched to RunPod but never finished.

    A worker process that dies mid-poll (crash, redeploy, OOM) loses the
    in-memory RunpodClient poll loop entirely — the RunPod job keeps running
    server-side and uploads to R2 regardless, but the DB row is left at
    status="pending" forever since it's never updated by anything. Job.
    runpod_job_id is only ever set right after a successful RunPod /run
    submission (see _persist_runpod_job_id above), so status=="pending" AND
    runpod_job_id is not None unambiguously identifies a job that was
    dispatched but whose worker died before the result came back — a fresh,
    never-dispatched job has runpod_job_id=None. Called once at worker
    startup (see worker_entrypoint.py) rather than from inside a normal job
    handler, since this is a sweep across all orphans, not a single job.
    """
    # Each orphan gets a short, bounded check rather than the full ~20min
    # poll ceiling — resume_orphaned_jobs runs before rq worker starts
    # accepting jobs, so a container that crashed with several jobs in
    # flight must not block startup for up to 20 minutes per orphan. A job
    # that's still genuinely running on RunPod after this many attempts is
    # left untouched (still "pending" with runpod_job_id set) — it'll be
    # picked up either by this same sweep on the next restart, or by
    # process_clip_job/process_image_job's own runpod_job_id guard the next
    # time RQ redelivers it, both of which poll rather than re-dispatch.
    RESUME_POLL_ATTEMPTS = 6  # ~30s at the default 5s poll_interval

    session, runpod_client, r2_client = _build_dependencies()
    try:
        orphans = session.query(Job).filter(
            Job.status == "pending", Job.runpod_job_id.isnot(None),
        ).all()
        for job in orphans:
            try:
                if job.type == "clip":
                    result = runpod_client.poll_video(job.runpod_job_id, max_attempts=RESUME_POLL_ATTEMPTS)
                    _finish_clip_job(session, r2_client, job, result)
                else:
                    result = runpod_client.poll_image(job.runpod_job_id, max_attempts=RESUME_POLL_ATTEMPTS)
                    _finish_image_job(session, r2_client, job, result)
            except TimeoutError:
                # Still running on RunPod's side, not a failure — leave it
                # pending for a later sweep or RQ redelivery to pick up.
                logger.info("orphaned job_id=%s still running on RunPod, will retry later", job.id)
                session.rollback()
            except Exception:
                # Not running inside an RQ job (this is a one-shot startup
                # sweep), so there's no RQ retry mechanism to hand the
                # exception to — mark it failed directly and move on to the
                # next orphan rather than reusing _handle_failure, which
                # always re-raises for RQ's benefit and would abort the
                # whole sweep after the first failure.
                logger.exception("failed to resume orphaned job_id=%s", job.id)
                session.rollback()
                job.retry_count += 1
                job.status = "failed"
                session.commit()
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

        try:
            if job.runpod_job_id is not None:
                # See the matching guard in process_clip_job: a prior
                # attempt already submitted this to RunPod — poll it instead
                # of dispatching a second, duplicate image job.
                result = runpod_client.poll_image(job.runpod_job_id)
            else:
                def _persist_runpod_job_id(runpod_job_id: str) -> None:
                    job.runpod_job_id = runpod_job_id
                    session.commit()

                result = runpod_client.dispatch_image(prompt=job.prompt, on_submitted=_persist_runpod_job_id)
            _finish_image_job(session, r2_client, job, result)
        except Exception as exc:
            _handle_failure(session, job, exc)
    finally:
        session.close()
