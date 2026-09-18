from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from reconcile_stuck_jobs import reconcile_stuck_jobs


class FakeJob:
    def __init__(self, id, type, runpod_job_id, updated_at):
        self.id = id
        self.type = type
        self.runpod_job_id = runpod_job_id
        self.updated_at = updated_at


def test_reconcile_finishes_a_completed_stuck_job():
    now = datetime.now(timezone.utc)
    stuck_job = FakeJob("j1", "clip", "rp-1", now - timedelta(minutes=10))
    query_stuck_jobs = MagicMock(return_value=[stuck_job])

    runpod_client = MagicMock()
    runpod_client.poll_video.return_value = {"output": {"key": "clips/x.mp4"}}
    finish_clip = MagicMock()
    finish_image = MagicMock()
    mark_failed = MagicMock()
    stale_before = now - timedelta(minutes=5)

    summary = reconcile_stuck_jobs(
        query_stuck_jobs, runpod_client, finish_clip, finish_image, mark_failed,
        stale_before=stale_before,
    )

    query_stuck_jobs.assert_called_once_with(stale_before)
    finish_clip.assert_called_once_with(stuck_job, {"output": {"key": "clips/x.mp4"}})
    finish_image.assert_not_called()
    mark_failed.assert_not_called()
    assert summary == {"finished": 1, "still_running": 0, "failed": 0}


def test_reconcile_leaves_still_running_job_alone():
    now = datetime.now(timezone.utc)
    stuck_job = FakeJob("j1", "clip", "rp-1", now - timedelta(minutes=10))
    query_stuck_jobs = MagicMock(return_value=[stuck_job])

    runpod_client = MagicMock()
    runpod_client.poll_video.side_effect = TimeoutError("still running")
    finish_clip = MagicMock()
    finish_image = MagicMock()
    mark_failed = MagicMock()

    summary = reconcile_stuck_jobs(
        query_stuck_jobs, runpod_client, finish_clip, finish_image, mark_failed,
        stale_before=now - timedelta(minutes=5),
    )

    finish_clip.assert_not_called()
    mark_failed.assert_not_called()
    assert summary == {"finished": 0, "still_running": 1, "failed": 0}


def test_reconcile_marks_failed_on_runpod_error():
    now = datetime.now(timezone.utc)
    stuck_job = FakeJob("j1", "image", "rp-2", now - timedelta(minutes=10))
    query_stuck_jobs = MagicMock(return_value=[stuck_job])

    runpod_client = MagicMock()
    runpod_client.poll_image.side_effect = RuntimeError("RunPod job failed: OOM")
    finish_clip = MagicMock()
    finish_image = MagicMock()
    mark_failed = MagicMock()

    summary = reconcile_stuck_jobs(
        query_stuck_jobs, runpod_client, finish_clip, finish_image, mark_failed,
        stale_before=now - timedelta(minutes=5),
    )

    mark_failed.assert_called_once()
    assert mark_failed.call_args[0][0] is stuck_job
    assert summary == {"finished": 0, "still_running": 0, "failed": 1}


def test_reconcile_uses_short_poll_attempts_not_full_ceiling():
    now = datetime.now(timezone.utc)
    stuck_job = FakeJob("j1", "clip", "rp-1", now - timedelta(minutes=10))
    query_stuck_jobs = MagicMock(return_value=[stuck_job])

    runpod_client = MagicMock()
    runpod_client.poll_video.return_value = {"output": {"key": "clips/x.mp4"}}
    finish_clip = MagicMock()

    reconcile_stuck_jobs(
        query_stuck_jobs, runpod_client, finish_clip, MagicMock(), MagicMock(),
        stale_before=now - timedelta(minutes=5), poll_attempts=6,
    )

    runpod_client.poll_video.assert_called_once_with("rp-1", max_attempts=6)


def test_reconcile_routes_image_jobs_to_poll_image_and_finish_image():
    now = datetime.now(timezone.utc)
    stuck_job = FakeJob("j1", "image", "rp-3", now - timedelta(minutes=10))
    query_stuck_jobs = MagicMock(return_value=[stuck_job])

    runpod_client = MagicMock()
    runpod_client.poll_image.return_value = {"output": {"key": "images/x.png"}}
    finish_clip = MagicMock()
    finish_image = MagicMock()

    reconcile_stuck_jobs(
        query_stuck_jobs, runpod_client, finish_clip, finish_image, MagicMock(),
        stale_before=now - timedelta(minutes=5),
    )

    runpod_client.poll_video.assert_not_called()
    finish_image.assert_called_once_with(stuck_job, {"output": {"key": "images/x.png"}})
    finish_clip.assert_not_called()
