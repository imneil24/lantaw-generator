import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Job, VideoProject, ProjectClip


def test_create_job_and_project_with_clips():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    project_id = str(uuid.uuid4())
    project = VideoProject(
        id=project_id, api_key_id="key1", target_duration=20.0,
        clip_count=2, status="pending",
    )
    session.add(project)

    job1 = Job(id=str(uuid.uuid4()), api_key_id="key1", type="clip",
               prompt="scene one", duration=10.0, status="pending", retry_count=0)
    job2 = Job(id=str(uuid.uuid4()), api_key_id="key1", type="clip",
               prompt="scene two", duration=10.0, status="pending", retry_count=0)
    session.add_all([job1, job2])
    session.flush()

    session.add(ProjectClip(project_id=project_id, sequence_index=0, job_id=job1.id))
    session.add(ProjectClip(project_id=project_id, sequence_index=1, job_id=job2.id))
    session.commit()

    fetched = session.query(VideoProject).filter_by(id=project_id).one()
    assert fetched.clip_count == 2
    clips = session.query(ProjectClip).filter_by(project_id=project_id).order_by(ProjectClip.sequence_index).all()
    assert [c.job_id for c in clips] == [job1.id, job2.id]
