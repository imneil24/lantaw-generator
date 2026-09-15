import math
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas import GenerateVideoRequest, ProjectResponse
from app.models import Job, VideoProject, ProjectClip

router = APIRouter()

PRO_TIER_CLIP_SECONDS = 10


def get_db_session():
    raise NotImplementedError("override in app wiring")


def get_moderation():
    raise NotImplementedError("override in app wiring")


def get_queue_enqueue():
    raise NotImplementedError("override in app wiring")


@router.post("/generate-video", status_code=status.HTTP_202_ACCEPTED, response_model=ProjectResponse)
def generate_video(
    body: GenerateVideoRequest,
    session=Depends(get_db_session),
    moderation=Depends(get_moderation),
    enqueue=Depends(get_queue_enqueue),
):
    result = moderation.check(body.prompt)
    if not result.allowed:
        raise HTTPException(status_code=422, detail="prompt rejected by moderation")

    clip_count = math.ceil(body.target_duration / PRO_TIER_CLIP_SECONDS)
    project_id = str(uuid.uuid4())
    project = VideoProject(id=project_id, target_duration=body.target_duration,
                            clip_count=clip_count, status="pending")
    session.add(project)

    job_ids = []
    for i in range(clip_count):
        job_id = str(uuid.uuid4())
        job_ids.append(job_id)
        job = Job(id=job_id, type="clip", prompt=body.prompt,
                  duration=float(PRO_TIER_CLIP_SECONDS), status="pending", retry_count=0)
        session.add(job)
        session.add(ProjectClip(project_id=project_id, sequence_index=i, job_id=job_id))

    session.commit()

    for job_id in job_ids:
        enqueue(job_id)

    return ProjectResponse(id=project_id, status="pending", clip_count=clip_count,
                            clips_complete=0, final_result_url=None)
