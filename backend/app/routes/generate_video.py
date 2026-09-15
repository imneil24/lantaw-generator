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

    enqueued_count = 0
    try:
        for job_id in job_ids:
            enqueue(job_id)
            enqueued_count += 1
    except Exception:
        # Some clips already have jobs in Postgres with no queued work behind
        # them (e.g. Redis dropped mid-loop) — mark the whole project and its
        # never-enqueued clips as failed rather than leaving them stuck at
        # "pending" forever with nothing that will ever process them.
        never_enqueued_ids = job_ids[enqueued_count:]
        if never_enqueued_ids:
            # synchronize_session="fetch" (not the default False) keeps the
            # in-memory Job objects already loaded in this session's
            # identity map (from the create loop above) in sync with the
            # bulk update — otherwise a later query in the same session can
            # return stale, pre-update objects for these same rows.
            session.query(Job).filter(Job.id.in_(never_enqueued_ids)).update(
                {"status": "failed"}, synchronize_session="fetch",
            )
        project.status = "failed"
        session.commit()
        raise HTTPException(status_code=503, detail="failed to queue video generation, please retry")

    return ProjectResponse(id=project_id, status="pending", clip_count=clip_count,
                            clips_complete=0, final_result_url=None)
