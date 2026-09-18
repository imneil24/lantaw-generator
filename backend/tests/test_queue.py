import uuid
from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Job, VideoProject, ProjectClip
from app.queue import process_clip_job, process_image_job, _extract_output


def test_extract_output_raises_on_completed_job_with_empty_output():
    # RunPod has been observed marking a job COMPLETED with an empty/None
    # output when the worker container crashes hard (e.g. OOM kill) instead
    # of the handler raising a catchable exception — output["key"] on that
    # would previously crash with a raw KeyError instead of a legible
    # failure message.
    with pytest.raises(RuntimeError, match="empty"):
        _extract_output({"output": None})
    with pytest.raises(RuntimeError, match="empty"):
        _extract_output({"output": {}})


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
    runpod_client.dispatch_video.return_value = {"output": {"key": "clips/x.mp4"}}
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_clip_job(job_id)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"
    assert updated.result_key == "clips/x.mp4"
    # worker-video/handler.py uploads the clip to R2 itself and returns only
    # the key — a full HD video base64-encoded into the job result is too
    # large for RunPod's own /job-done callback (rejected with a 400), so
    # the backend must not expect or re-upload raw bytes for video jobs.
    r2_client.upload.assert_not_called()


def test_process_clip_job_persists_runpod_job_id_as_soon_as_submitted():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="pending", retry_count=0))
    session.commit()

    def fake_dispatch_video(prompt, duration, on_submitted=None):
        if on_submitted is not None:
            on_submitted("runpod-job-abc")
        return {"output": {"key": "clips/x.mp4"}}

    runpod_client = MagicMock()
    runpod_client.dispatch_video.side_effect = fake_dispatch_video
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_clip_job(job_id)

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.runpod_job_id == "runpod-job-abc"


def test_process_clip_job_marks_failed_and_reraises_when_rq_has_no_retries_left():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="pending", retry_count=2))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.side_effect = RuntimeError("upstream down")
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)), \
         patch("app.queue._is_final_attempt", return_value=True):
        # Still re-raises even on the final attempt — RQ's own Retry
        # mechanism (not this function) decides not to re-enqueue based on
        # its internal retries_left, and needs the exception to record the
        # job as failed in its own FailedJobRegistry.
        try:
            process_clip_job(job_id)
            assert False, "expected RuntimeError to propagate to RQ"
        except RuntimeError:
            pass

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "failed"
    assert updated.retry_count == 3


def test_process_clip_job_reraises_and_increments_retry_count_when_rq_has_retries_left():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="pending", retry_count=0))
    session.commit()

    runpod_client = MagicMock()
    runpod_client.dispatch_video.side_effect = RuntimeError("upstream down")
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)), \
         patch("app.queue._is_final_attempt", return_value=False):
        try:
            process_clip_job(job_id)
            assert False, "expected RuntimeError to propagate so RQ retries the job"
        except RuntimeError:
            pass

    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.retry_count == 1
    assert updated.status == "pending"


def test_is_final_attempt_true_outside_rq_worker_context():
    from app.queue import _is_final_attempt
    with patch("app.queue.get_current_job", return_value=None):
        assert _is_final_attempt() is True


def test_is_final_attempt_reads_rq_retries_left():
    from app.queue import _is_final_attempt
    fake_job = MagicMock(retries_left=2)
    with patch("app.queue.get_current_job", return_value=fake_job):
        assert _is_final_attempt() is False
    fake_job.retries_left = 0
    with patch("app.queue.get_current_job", return_value=fake_job):
        assert _is_final_attempt() is True


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
    runpod_client.dispatch_video.return_value = {"output": {"key": "clips/b.mp4"}}
    r2_client = MagicMock()
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
    # worker-video/handler.py uploads each clip to R2 itself; the backend
    # only uploads the stitched final video.
    assert r2_client.upload.call_count == 1


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
    runpod_client.dispatch_video.return_value = {"output": {"key": "clips/a.mp4"}}
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)), \
         patch("app.queue.stitch_project") as mock_stitch:
        process_clip_job(job1_id)

    mock_stitch.assert_not_called()
    project = session.query(VideoProject).filter_by(id=project_id).one()
    assert project.status == "pending"


def test_process_clip_job_skips_dispatch_when_already_complete():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="complete",
                     retry_count=0, result_key="clips/already.mp4"))
    session.commit()

    runpod_client = MagicMock()
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_clip_job(job_id)  # redelivered/duplicate job

    runpod_client.dispatch_video.assert_not_called()
    r2_client.upload.assert_not_called()


def test_process_image_job_skips_dispatch_when_already_complete():
    session = _make_session()
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="image", prompt="p", duration=None, status="complete",
                     retry_count=0, result_key="images/already.png"))
    session.commit()

    runpod_client = MagicMock()
    r2_client = MagicMock()

    with patch("app.queue._build_dependencies", return_value=(session, runpod_client, r2_client)):
        process_image_job(job_id)

    runpod_client.dispatch_image.assert_not_called()
    r2_client.upload.assert_not_called()


def test_maybe_stitch_project_does_not_restitch_already_complete_project():
    session = _make_session()

    project_id = str(uuid.uuid4())
    session.add(VideoProject(id=project_id, target_duration=10.0, clip_count=1, status="complete",
                              final_result_key="videos/already-final.mp4"))
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="complete",
                     retry_count=0, result_key="clips/a.mp4"))
    session.add(ProjectClip(project_id=project_id, sequence_index=0, job_id=job_id))
    session.commit()

    from app.queue import _maybe_stitch_project
    r2_client = MagicMock()
    job = session.query(Job).filter_by(id=job_id).one()

    with patch("app.queue.stitch_project") as mock_stitch:
        _maybe_stitch_project(session, r2_client, job)

    mock_stitch.assert_not_called()
    project = session.query(VideoProject).filter_by(id=project_id).one()
    assert project.final_result_key == "videos/already-final.mp4"  # untouched


def test_maybe_stitch_project_handles_missing_sibling_job_gracefully():
    session = _make_session()

    project_id = str(uuid.uuid4())
    session.add(VideoProject(id=project_id, target_duration=20.0, clip_count=2, status="pending"))
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="complete",
                     retry_count=0, result_key="clips/a.mp4"))
    session.add(ProjectClip(project_id=project_id, sequence_index=0, job_id=job_id))
    # sibling clip references a job_id with no matching Job row
    session.add(ProjectClip(project_id=project_id, sequence_index=1, job_id=str(uuid.uuid4())))
    session.commit()

    from app.queue import _maybe_stitch_project
    r2_client = MagicMock()
    job = session.query(Job).filter_by(id=job_id).one()

    with patch("app.queue.stitch_project") as mock_stitch:
        _maybe_stitch_project(session, r2_client, job)  # does not raise KeyError

    mock_stitch.assert_not_called()
    project = session.query(VideoProject).filter_by(id=project_id).one()
    assert project.status == "pending"


def test_maybe_stitch_project_failure_does_not_affect_clip_job_status():
    session = _make_session()

    project_id = str(uuid.uuid4())
    session.add(VideoProject(id=project_id, target_duration=10.0, clip_count=1, status="pending"))
    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="clip", prompt="p", duration=10.0, status="complete",
                     retry_count=0, result_key="clips/a.mp4"))
    session.add(ProjectClip(project_id=project_id, sequence_index=0, job_id=job_id))
    session.commit()

    from app.queue import _maybe_stitch_project
    r2_client = MagicMock()
    r2_client.download.side_effect = RuntimeError("R2 is down")
    job = session.query(Job).filter_by(id=job_id).one()

    _maybe_stitch_project(session, r2_client, job)  # swallows the error, does not raise

    updated_job = session.query(Job).filter_by(id=job_id).one()
    assert updated_job.status == "complete"  # clip job unaffected by stitch failure
    project = session.query(VideoProject).filter_by(id=project_id).one()
    assert project.status == "pending"  # stitch never completed, but nothing corrupted


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
