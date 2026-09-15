import uuid
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.models import Base, Job, VideoProject, ProjectClip
from app.routes.jobs import router, get_db_session, get_r2_client


def _build_app_with_data():
    app = FastAPI()
    app.include_router(router)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()

    job_id = str(uuid.uuid4())
    session.add(Job(id=job_id, type="image", prompt="p",
                     duration=None, status="complete", retry_count=0, result_key="images/x.png"))

    project_id = str(uuid.uuid4())
    session.add(VideoProject(id=project_id, target_duration=10.0,
                              clip_count=1, status="complete", final_result_key="videos/final.mp4"))
    clip_job_id = str(uuid.uuid4())
    session.add(Job(id=clip_job_id, type="clip", prompt="p",
                     duration=10.0, status="complete", retry_count=0, result_key="clips/c0.mp4"))
    session.flush()
    session.add(ProjectClip(project_id=project_id, sequence_index=0, job_id=clip_job_id))
    session.commit()

    class FakeR2:
        def signed_url(self, key, expires_in=3600):
            return f"https://signed.example.com/{key}"

    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_r2_client] = lambda: FakeR2()
    return app, job_id, project_id


def test_get_job_returns_signed_url():
    app, job_id, _ = _build_app_with_data()
    client = TestClient(app)
    response = client.get(f"/jobs/{job_id}", headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete"
    assert body["result_url"] == "https://signed.example.com/images/x.png"


def test_get_job_unknown_id_returns_404():
    app, _, _ = _build_app_with_data()
    client = TestClient(app)
    response = client.get(f"/jobs/{uuid.uuid4()}", headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 404


def test_get_project_returns_progress_and_final_url():
    app, _, project_id = _build_app_with_data()
    client = TestClient(app)
    response = client.get(f"/projects/{project_id}", headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 200
    body = response.json()
    assert body["clip_count"] == 1
    assert body["clips_complete"] == 1
    assert body["final_result_url"] == "https://signed.example.com/videos/final.mp4"
