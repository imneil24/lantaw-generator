import uuid
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Job, VideoProject, ProjectClip
from app.queue import process_clip_job, process_image_job


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def test_process_clip_job_marks_complete_on_success():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="pending", retry_count=0))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.return_value = {"output": {"key": "clips/x.mp4", "bytes_b64": "AA=="}}
    r2_client = MagicMock()
    r2_client.upload.return_value = "clips/x.mp4"

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_clip_job(job_id)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"
    assert updated.result_key == "clips/x.mp4"


def test_process_clip_job_marks_failed_after_max_retries():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="pending", retry_count=3))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.side_effect = RuntimeError("upstream down")
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_clip_job(job_id)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "failed"


def test_process_clip_job_reraises_and_increments_retry_count_below_max():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="pending", retry_count=0))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.side_effect = RuntimeError("upstream down")
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        try:
            process_clip_job(job_id)
            assert False, "expected RuntimeError to propagate so RQ retries the job"
        except RuntimeError:
            pass

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.retry_count == 1
    assert updated.status == "pending"


def test_process_clip_job_missing_row_returns_without_raising():
    session = _make_session()
    runpod_client = MagicMock()
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_clip_job(str(uuid.uuid4()))  # does not raise NoResultFound


def test_process_clip_job_triggers_stitch_when_last_clip_in_project_completes(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tts.NullTTSProvider.generate", lambda self, text, target_duration: str(tmp_path / "silence.wav"))
    session = _make_session()

    project_id = str(uuid.uuid4())
    session.add(VideoProject(id=project_id, target_duration=10.0, clip_count=2, status="pending"))

    job1_id = str(uuid.uuid4())
    job2_id = str(uuid.uuid4())
    session.add(Job(id=job1_id, type="clip", prompt="p", duration=10.0, status="complete",
                     retry_count=0, result_key="clips/a.mp4"))
    session.add(Job(id=job2_id, type="clip", prompt="p", duration=10.0, status="pending", retry_count=0))
    session.add(ProjectClip(project_id=project_id, sequence_index=0, job_id=job1_id))
    session.add(ProjectClip(project_id=project_id, sequence_index=1, job_id=job2_id))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.return_value = {"output": {"key": "clips/b.mp4", "bytes_b64": "AA=="}}
    r2_client = MagicMock()
    r2_client.upload.return_value = "clips/b.mp4"
    r2_client.download.return_value = b"fake-clip-bytes"

    def fake_stitch(clip_paths, audio_path, output_path):
        open(output_path, "wb").write(b"final-video-bytes")

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)), \
         patch("app.queue.stitch_project", side_effect=fake_stitch) as mock_stitch:
        process_clip_job(job2_id)

    mock_stitch.assert_called_once()
    project = session.query(VideoProject).filter_by(id=project_id).one()
    assert project.status == "complete"
    assert project.final_result_key is not None
    assert r2_client.upload.call_count == 2  # clip upload + final stitched video upload


def test_process_clip_job_does_not_stitch_when_sibling_clips_still_pending():
    session = _make_session()

    project_id = str(uuid.uuid4())
    session.add(VideoProject(id=project_id, target_duration=20.0, clip_count=2, status="pending"))

    job1_id = str(uuid.uuid4())
    job2_id = str(uuid.uuid4())
    session.add(Job(id=job1_id, type="clip", prompt="p", duration=10.0, status="pending", retry_count=0))
    session.add(Job(id=job2_id, type="clip", prompt="p", duration=10.0, status="pending", retry_count=0))
    session.add(ProjectClip(project_id=project_id, sequence_index=0, job_id=job1_id))
    session.add(ProjectClip(project_id=project_id, sequence_index=1, job_id=job2_id))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.return_value = {"output": {"key": "clips/a.mp4", "bytes_b64": "AA=="}}
    r2_client = MagicMock()
    r2_client.upload.return_value = "clips/a.mp4"

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)), \
         patch("app.queue.stitch_project") as mock_stitch:
        process_clip_job(job1_id)

    mock_stitch.assert_not_called()
    project = session.query(VideoProject).filter_by(id=project_id).one()
    assert project.status == "pending"


def test_process_image_job_marks_complete_on_success():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="image", prompt="p", duration=None, status="pending", retry_count=0))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_image.return_value = {"output": {"key": "images/x.png", "bytes_b64": "AA=="}}
    r2_client = MagicMock()
    r2_client.upload.return_value = "images/x.png"

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_image_job(job_id)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"
    assert updated.result_key == "images/x.png"
