from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.models import Base, Job, VideoProject, ProjectClip
from app.routes.generate_video import router, get_db_session, get_moderation, get_queue_enqueue


def _build_app():
    app = FastAPI()
    app.include_router(router)

    # StaticPool: see test_routes_image.py for why plain :memory: breaks
    # under TestClient's separate-thread execution.
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    class AllowAllModeration:
        def check(self, prompt):
            from app.moderation import ModerationResult
            return ModerationResult(allowed=True)

    enqueued = []

    def fake_enqueue(job_id):
        enqueued.append(job_id)

    session_holder = {"session": Session()}
    app.dependency_overrides[get_db_session] = lambda: session_holder["session"]
    app.dependency_overrides[get_moderation] = lambda: AllowAllModeration()
    app.dependency_overrides[get_queue_enqueue] = lambda: fake_enqueue
    return app, enqueued, session_holder


def test_generate_video_splits_into_clip_jobs():
    app, enqueued, session_holder = _build_app()
    client = TestClient(app)
    response = client.post(
        "/generate-video", json={"prompt": "a journey through a forest", "target_duration": 25},
        headers={"X-Api-Key-Id": "primary"},
    )
    assert response.status_code == 202
    body = response.json()
    project_id = body["id"]
    assert body["clip_count"] == 3  # ceil(25/10)
    assert len(enqueued) == 3

    session = session_holder["session"]
    project = session.query(VideoProject).filter_by(id=project_id).one()
    assert project.status == "pending"
    clips = session.query(ProjectClip).filter_by(project_id=project_id).order_by(ProjectClip.sequence_index).all()
    assert [c.sequence_index for c in clips] == [0, 1, 2]


def test_generate_video_rejects_out_of_range_duration():
    app, enqueued, _ = _build_app()
    client = TestClient(app)
    response = client.post(
        "/generate-video", json={"prompt": "x", "target_duration": -5},
        headers={"X-Api-Key-Id": "primary"},
    )
    assert response.status_code == 422
    assert len(enqueued) == 0


def test_generate_video_marks_project_and_unqueued_clips_failed_on_partial_enqueue_failure():
    app = FastAPI()
    app.include_router(router)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()

    class AllowAllModeration:
        def check(self, prompt):
            from app.moderation import ModerationResult
            return ModerationResult(allowed=True)

    enqueued = []

    def flaky_enqueue(job_id):
        if len(enqueued) >= 2:
            raise ConnectionError("redis is down")
        enqueued.append(job_id)

    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_moderation] = lambda: AllowAllModeration()
    app.dependency_overrides[get_queue_enqueue] = lambda: flaky_enqueue

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/generate-video", json={"prompt": "a long journey", "target_duration": 30},  # 3 clips
        headers={"X-Api-Key-Id": "primary"},
    )

    assert response.status_code == 503
    assert len(enqueued) == 2  # 2 succeeded before the 3rd raised

    projects = session.query(VideoProject).all()
    assert len(projects) == 1
    assert projects[0].status == "failed"

    jobs = session.query(Job).order_by(Job.id).all()
    assert len(jobs) == 3
    failed_jobs = [j for j in jobs if j.status == "failed"]
    assert len(failed_jobs) == 1  # only the clip whose enqueue never succeeded
