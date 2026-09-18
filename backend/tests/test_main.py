import os
import uuid
from fastapi.testclient import TestClient


def _set_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNPOD_VIDEO_KEY", "vkey")
    monkeypatch.setenv("RUNPOD_IMAGE_KEY", "ikey")
    monkeypatch.setenv("RUNPOD_VIDEO_ENDPOINT", "https://api.runpod.ai/v2/vid/runsync")
    monkeypatch.setenv("RUNPOD_IMAGE_ENDPOINT", "https://api.runpod.ai/v2/img/runsync")
    monkeypatch.setenv("R2_WRITE_KEY", "wkey")
    monkeypatch.setenv("R2_WRITE_SECRET", "wsecret")
    monkeypatch.setenv("R2_READ_KEY", "rkey")
    monkeypatch.setenv("R2_READ_SECRET", "rsecret")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.setenv("R2_ENDPOINT", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setenv("POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    from app.security import hash_api_key
    monkeypatch.setenv("BACKEND_API_KEY_HASH", hash_api_key("test-key"))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("RUNPOD_WEBHOOK_SECRET", "test-webhook-secret")


def test_settings_loads_public_base_url_and_runpod_webhook_secret(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    from app.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.public_base_url == "https://api.example.com"
    assert settings.runpod_webhook_secret == "test-webhook-secret"
    assert settings.runpod_webhook_secret in settings.all_secrets()


def test_webhook_route_via_create_app_does_not_require_auth_header(monkeypatch, tmp_path):
    # Regression guard: app-level auth_dependency must not apply to the
    # mounted webhook sub-app (see main.py's comment on the mount).
    _set_env(monkeypatch, tmp_path)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    client = TestClient(app)
    # No Authorization header at all — a request that would 401 on any
    # normal route (e.g. GET /jobs/{id}) must not 401 here.
    response = client.post("/webhooks/runpod/wrong-secret/00000000-0000-0000-0000-000000000000",
                            json={"id": "x", "status": "COMPLETED", "output": {}})
    assert response.status_code == 404  # 404 (wrong secret), not 401 (auth)


def test_unauthenticated_request_rejected(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    client = TestClient(app)
    response = client.post("/generate-image", json={"prompt": "a fox"})
    assert response.status_code == 401


def test_wrong_key_rejected(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    client = TestClient(app)
    response = client.post("/generate-image", json={"prompt": "a fox"},
                            headers={"Authorization": "Bearer wrong-key"})
    assert response.status_code == 401


def test_route_added_without_explicit_dependencies_still_requires_auth(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()

    @app.get("/__new-route-nobody-remembered-to-protect")
    def unprotected_by_omission():
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/__new-route-nobody-remembered-to-protect")
    assert response.status_code == 401


def test_unhandled_exception_returns_generic_500_with_correlation_id(monkeypatch, tmp_path):
    _set_env(monkeypatch, tmp_path)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()

    @app.get("/__boom")
    def boom():
        raise ValueError("internal secret detail: sk-abc123")

    client = TestClient(app, raise_server_exceptions=False)
    # App-level dependencies (auth, rate limit) now apply to every route,
    # including ones registered after create_app() returns — this route
    # would otherwise 401 before ever reaching the handler under test.
    response = client.get("/__boom", headers={"Authorization": "Bearer test-key"})
    assert response.status_code == 500
    body = response.json()
    assert "sk-abc123" not in response.text
    assert "correlation_id" in body
