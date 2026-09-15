import base64
from app.models import Job

MAX_RETRIES = 3


def _mark_failed_or_retry(session, job: Job) -> None:
    if job.retry_count >= MAX_RETRIES:
        job.status = "failed"
    else:
        job.retry_count += 1
    session.commit()


def process_clip_job(job_id: str, session_factory, runpod_client, r2_client) -> None:
    session = session_factory()
    job = session.query(Job).filter_by(id=job_id).one()

    try:
        result = runpod_client.dispatch_video(prompt=job.prompt, duration=job.duration)
        raw_bytes = base64.b64decode(result["output"]["bytes_b64"])
        key = result["output"]["key"]
        r2_client.upload(key, raw_bytes, "video/mp4")
        job.status = "complete"
        job.result_key = key
        session.commit()
    except Exception:
        _mark_failed_or_retry(session, job)


def process_image_job(job_id: str, session_factory, runpod_client, r2_client) -> None:
    session = session_factory()
    job = session.query(Job).filter_by(id=job_id).one()

    try:
        result = runpod_client.dispatch_image(prompt=job.prompt)
        raw_bytes = base64.b64decode(result["output"]["bytes_b64"])
        key = result["output"]["key"]
        r2_client.upload(key, raw_bytes, "image/png")
        job.status = "complete"
        job.result_key = key
        session.commit()
    except Exception:
        _mark_failed_or_retry(session, job)
