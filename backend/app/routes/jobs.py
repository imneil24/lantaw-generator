from fastapi import APIRouter, Depends, HTTPException
from app.models import Job, VideoProject, ProjectClip
from app.schemas import JobResponse, ProjectResponse

router = APIRouter()


def get_db_session():
    raise NotImplementedError("override in app wiring")


def get_r2_client():
    raise NotImplementedError("override in app wiring")


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, session=Depends(get_db_session), r2_client=Depends(get_r2_client)):
    job = session.query(Job).filter_by(id=job_id).one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    result_url = r2_client.signed_url(job.result_key) if job.result_key else None

    queue_position = None
    if job.status == "pending":
        queue_position = session.query(Job).filter(
            Job.type == job.type, Job.status == "pending", Job.created_at < job.created_at,
        ).count()

    return JobResponse(id=job.id, type=job.type, status=job.status, result_url=result_url,
                        created_at=job.created_at, queue_position=queue_position)


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, session=Depends(get_db_session), r2_client=Depends(get_r2_client)):
    project = session.query(VideoProject).filter_by(id=project_id).one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    clips = session.query(ProjectClip).filter_by(project_id=project_id).all()
    job_ids = [c.job_id for c in clips]
    complete_count = session.query(Job).filter(Job.id.in_(job_ids), Job.status == "complete").count() if job_ids else 0

    final_url = r2_client.signed_url(project.final_result_key) if project.final_result_key else None
    return ProjectResponse(id=project.id, status=project.status, clip_count=project.clip_count,
                            clips_complete=complete_count, final_result_url=final_url)
