from datetime import datetime
from typing import Callable

# Real wiring (not exercised by this module's own test suite, which only
# tests the pure function below): constructing a SQLAlchemy session against
# backend's Job model, a RunpodClient from backend.app.runpod_client, and
# backend's _finish_clip_job/_finish_image_job/_mark_job_failed_non_raising
# is a cross-package concern — ops/ has no dependency on the backend
# package (see repo-layout notes in CLAUDE.md: each component has its own
# requirements.txt). The cron invocation script that wires this function up
# must set PYTHONPATH to include backend/ at deploy time; that's an
# infra/deploy concern, not something this module does itself.


def reconcile_stuck_jobs(query_stuck_jobs: Callable[[datetime], list], runpod_client,
                          finish_clip: Callable, finish_image: Callable, mark_failed: Callable,
                          stale_before: datetime, poll_attempts: int = 6) -> dict:
    """Recovers jobs whose RunPod webhook callback never arrived.

    query_stuck_jobs(stale_before) returns Job-like rows (duck-typed:
    .id/.type/.runpod_job_id) with status="dispatched" older than
    stale_before — injected rather than querying via SQLAlchemy directly
    here, since ops/ is an independently-versioned component with no
    dependency on the backend package.

    Does a short bounded poll of RunPod's status for each stuck job (not
    the full ~20 minute ceiling — this is a safety net running on a cron
    cadence, not the primary completion path, so it must not block the
    cron job for a long time per stuck job).
    """
    stuck_jobs = query_stuck_jobs(stale_before)

    summary = {"finished": 0, "still_running": 0, "failed": 0}
    for job in stuck_jobs:
        poll = runpod_client.poll_video if job.type == "clip" else runpod_client.poll_image
        finish = finish_clip if job.type == "clip" else finish_image
        try:
            result = poll(job.runpod_job_id, max_attempts=poll_attempts)
        except TimeoutError:
            summary["still_running"] += 1
            continue
        except Exception as exc:
            mark_failed(job, exc)
            summary["failed"] += 1
            continue

        finish(job, result)
        summary["finished"] += 1

    return summary
