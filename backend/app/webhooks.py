import hmac
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from app.models import Job
from app.queue import _finish_webhook_result

logger = logging.getLogger(__name__)
router = APIRouter()


def get_db_session():
    raise NotImplementedError("override in app wiring")


def get_r2_client():
    raise NotImplementedError("override in app wiring")


def get_webhook_secret():
    raise NotImplementedError("override in app wiring")


@router.post("/runpod/{secret}/{job_id}")
async def runpod_webhook(secret: str, job_id: str, request: Request,
                          session=Depends(get_db_session), r2_client=Depends(get_r2_client),
                          expected_secret: str = Depends(get_webhook_secret)):
    # Constant-time compare: this path segment is the only gate on this
    # endpoint (RunPod's callback carries no bearer key), so a
    # timing-based secret recovery here would fully defeat it.
    if not hmac.compare_digest(secret, expected_secret):
        raise HTTPException(status_code=404)

    job = session.query(Job).filter_by(id=job_id).one_or_none()
    if job is None:
        raise HTTPException(status_code=404)

    body = await request.json()
    runpod_job_id = body.get("id")
    # Never trust the callback body's identity over our own lookup — the
    # secret alone doesn't bind this request to *this* job's specific
    # RunPod dispatch, only to our deploy in general.
    if job.runpod_job_id is not None and runpod_job_id != job.runpod_job_id:
        logger.warning("webhook job_id=%s runpod_job_id mismatch: got %s, expected %s",
                        job_id, runpod_job_id, job.runpod_job_id)
        raise HTTPException(status_code=404)

    if job.status in ("complete", "failed"):
        # RunPod may retry webhook delivery on transient failures on its
        # end — this must be idempotent, not an error.
        return {"ok": True}

    status = body.get("status")
    _finish_webhook_result(session, r2_client, job, status, body)
    return {"ok": True}
