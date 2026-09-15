import uuid
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Job
from app.queue import process_clip_job, process_image_job


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_process_clip_job_marks_complete_on_success():
    Session = _make_session_factory()
    session = Session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, api_key_id="k", type="clip", prompt="p", duration=10.0, status="pending", retry_count=0))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.return_value = {"output": {"key": "clips/x.mp4", "bytes_b64": "AA=="}}
    r2_client = MagicMock()
    r2_client.upload.return_value = "clips/x.mp4"

    process_clip_job(job_id, Session, runpod_client, r2_client)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"
    assert updated.result_key == "clips/x.mp4"


def test_process_clip_job_marks_failed_after_max_retries():
    Session = _make_session_factory()
    session = Session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, api_key_id="k", type="clip", prompt="p", duration=10.0, status="pending", retry_count=3))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.side_effect = RuntimeError("upstream down")
    r2_client = MagicMock()

    process_clip_job(job_id, Session, runpod_client, r2_client)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "failed"


def test_process_image_job_marks_complete_on_success():
    Session = _make_session_factory()
    session = Session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, api_key_id="k", type="image", prompt="p", duration=None, status="pending", retry_count=0))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_image.return_value = {"output": {"key": "images/x.png", "bytes_b64": "AA=="}}
    r2_client = MagicMock()
    r2_client.upload.return_value = "images/x.png"

    process_image_job(job_id, Session, runpod_client, r2_client)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"
    assert updated.result_key == "images/x.png"
