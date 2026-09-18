"""Entrypoint for the rq-worker container/service.

Runs resume_orphaned_jobs() once before handing off to RQ's own worker CLI,
so jobs left stranded by a prior crash/redeploy (dispatched to RunPod,
never polled to completion) get picked back up instead of sitting at
status="pending" forever. See queue.py's resume_orphaned_jobs for why this
has to run here rather than inside a normal job handler.
"""
import logging
import os
import sys

from app.logging_conf import configure_logging
from app.config import get_settings
from app.db import get_engine, apply_column_additions
from app.queue import resume_orphaned_jobs

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.all_secrets())

    # The backend service normally owns schema setup (Base.metadata.
    # create_all + apply_column_additions in main.py), but this worker can
    # boot and query Job rows (resume_orphaned_jobs, below) before or
    # without a backend deploy ever having run against this database —
    # applying column additions here too keeps the worker resilient to
    # that ordering instead of assuming the backend already ran first.
    apply_column_additions(get_engine(settings.postgres_dsn))

    logger.info("resuming orphaned jobs before starting rq worker")
    resume_orphaned_jobs()
    logger.info("orphan resume complete, starting rq worker")

    # Replaces this process with rq's own CLI entrypoint (same mechanism as
    # exec in a shell script) rather than subprocess.run, so RQ still owns
    # PID 1's signal handling (SIGTERM on Railway redeploy/restart) instead
    # of a wrapper process that would need to forward signals itself.
    os.execvp("rq", ["rq", "worker", *sys.argv[1:]])


if __name__ == "__main__":
    main()
