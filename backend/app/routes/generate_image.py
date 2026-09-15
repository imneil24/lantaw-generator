import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas import GenerateImageRequest, JobResponse
from app.models import Job

router = APIRouter()


def get_db_session():
    raise NotImplementedError("override in app wiring")


def get_moderation():
    raise NotImplementedError("override in app wiring")


def get_queue_enqueue():
    raise NotImplementedError("override in app wiring")


@router.post("/generate-image", status_code=status.HTTP_202_ACCEPTED, response_model=JobResponse)
def generate_image(
    body: GenerateImageRequest,
    session=Depends(get_db_session),
    moderation=Depends(get_moderation),
    enqueue=Depends(get_queue_enqueue),
):
    result = moderation.check(body.prompt)
    if not result.allowed:
        raise HTTPException(status_code=422, detail="prompt rejected by moderation")

    job_id = str(uuid.uuid4())
    job = Job(id=job_id, type="image", prompt=body.prompt,
              duration=None, status="pending", retry_count=0)
    session.add(job)
    session.commit()
    enqueue(job_id)

    return JobResponse(id=job_id, type="image", status="pending", result_url=None, created_at=job.created_at)
