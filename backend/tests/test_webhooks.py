import uuid
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import MagicMock
from app.models import Base, Job
from app.webhooks import router, get_db_session, get_r2_client, get_webhook_secret

SECRET = "test-webhook-secret"


def _build_app_with_job(status="dispatched", runpod_job_id="rp-abc", job_type="clip"):
    # Mirrors main.py's real wiring: the webhook router is mounted as a
    # separate sub-application at /webhooks so it does NOT inherit the
    # parent app's constructor-level auth/rate-limit dependencies (which
    # apply to every route on that FastAPI instance, including ones added
    # via include_router — see main.py's comment on this).
    app = FastAPI()
    webhook_app = FastAPI()
    webhook_app.include_router(router)
    app.mount("/webhooks", webhook_app)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()

    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type=job_type, prompt="p", duration=10.0 if job_type == "clip" else None,
                     status=status, retry_count=0, runpod_job_id=runpod_job_id))
    session.commit()

    r2_client = MagicMock()
    webhook_app.dependency_overrides[get_db_session] = lambda: session
    webhook_app.dependency_overrides[get_r2_client] = lambda: r2_client
    webhook_app.dependency_overrides[get_webhook_secret] = lambda: SECRET
    return app, session, job_id, r2_client


def test_webhook_wrong_secret_returns_404_and_does_not_change_job():
    app, session, job_id, _ = _build_app_with_job()
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/wrong-secret/{job_id}",
        json={"id": "rp-abc", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}},
    )
    assert response.status_code == 404
    assert session.query(Job).filter_by(id=job_id).one().status == "dispatched"


def test_webhook_unknown_job_id_returns_404():
    app, _, _, _ = _build_app_with_job()
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{uuid.uuid4()}",
        json={"id": "rp-abc", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}},
    )
    assert response.status_code == 404


def test_webhook_runpod_job_id_mismatch_returns_404_and_does_not_change_job():
    app, session, job_id, _ = _build_app_with_job(runpod_job_id="rp-abc")
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{job_id}",
        json={"id": "rp-DIFFERENT", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}},
    )
    assert response.status_code == 404
    assert session.query(Job).filter_by(id=job_id).one().status == "dispatched"


def test_webhook_already_complete_job_is_a_noop():
    app, session, job_id, r2_client = _build_app_with_job(status="complete")
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{job_id}",
        json={"id": "rp-abc", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}},
    )
    assert response.status_code == 200
    r2_client.upload.assert_not_called()


def test_webhook_completed_clip_job_finishes_it():
    app, session, job_id, r2_client = _build_app_with_job(job_type="clip")
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{job_id}",
        json={"id": "rp-abc", "status": "COMPLETED", "output": {"key": "clips/x.mp4"}},
    )
    assert response.status_code == 200
    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"
    assert updated.result_key == "clips/x.mp4"


def test_webhook_completed_image_job_finishes_it():
    app, session, job_id, r2_client = _build_app_with_job(job_type="image")
    r2_client.upload.return_value = "images/x.png"
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{job_id}",
        json={"id": "rp-abc", "status": "COMPLETED", "output": {"key": "images/x.png", "bytes_b64": "AA=="}},
    )
    assert response.status_code == 200
    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "complete"


def test_webhook_failed_status_marks_job_failed():
    app, session, job_id, _ = _build_app_with_job(job_type="image")
    client = TestClient(app)
    response = client.post(
        f"/webhooks/runpod/{SECRET}/{job_id}",
        json={"id": "rp-abc", "status": "FAILED", "error": "OOM"},
    )
    assert response.status_code == 200
    updated = session.query(Job).filter_by(id=job_id).one()
    assert updated.status == "failed"
    assert updated.retry_count == 1
