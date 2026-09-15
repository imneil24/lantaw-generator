import uuid
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.models import Base
from app.routes.generate_image import router, get_db_session, get_moderation, get_queue_enqueue


def _build_app():
    app = FastAPI()
    app.include_router(router)

    # StaticPool keeps a single shared connection so the in-memory SQLite DB
    # is visible across threads — TestClient runs the app in a different
    # thread than the one that calls create_all(), and SQLite's default
    # SingletonThreadPool gives each thread its own (separately empty) DB.
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

    app.dependency_overrides[get_db_session] = lambda: Session()
    app.dependency_overrides[get_moderation] = lambda: AllowAllModeration()
    app.dependency_overrides[get_queue_enqueue] = lambda: fake_enqueue
    return app, enqueued


def test_generate_image_creates_job_and_enqueues():
    app, enqueued = _build_app()
    client = TestClient(app)
    response = client.post("/generate-image", json={"prompt": "a red fox"}, headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 202
    body = response.json()
    assert "id" in body
    assert body["status"] == "pending"
    assert len(enqueued) == 1


def test_generate_image_rejects_blocked_prompt():
    app, enqueued = _build_app()

    class BlockAllModeration:
        def check(self, prompt):
            from app.moderation import ModerationResult
            return ModerationResult(allowed=False, reason="blocked term: x")

    app.dependency_overrides[get_moderation] = lambda: BlockAllModeration()
    client = TestClient(app)
    response = client.post("/generate-image", json={"prompt": "bad prompt"}, headers={"X-Api-Key-Id": "primary"})
    assert response.status_code == 422
    assert len(enqueued) == 0


def test_generate_image_rejects_extra_fields():
    app, _ = _build_app()
    client = TestClient(app)
    response = client.post(
        "/generate-image",
        json={"prompt": "a fox", "image_url": "http://evil.example.com"},
        headers={"X-Api-Key-Id": "primary"},
    )
    assert response.status_code == 422
