import pytest
from fastapi import HTTPException
from app.middleware.auth import make_auth_dependency
from app.security import hash_api_key


def test_valid_bearer_key_passes():
    stored_hash = hash_api_key("real-key")
    dependency = make_auth_dependency(stored_hash=stored_hash)
    result = dependency(authorization="Bearer real-key")
    assert result == "primary"


def test_missing_header_rejected():
    stored_hash = hash_api_key("real-key")
    dependency = make_auth_dependency(stored_hash=stored_hash)
    with pytest.raises(HTTPException) as exc_info:
        dependency(authorization=None)
    assert exc_info.value.status_code == 401


def test_wrong_key_rejected():
    stored_hash = hash_api_key("real-key")
    dependency = make_auth_dependency(stored_hash=stored_hash)
    with pytest.raises(HTTPException) as exc_info:
        dependency(authorization="Bearer wrong-key")
    assert exc_info.value.status_code == 401


def test_malformed_header_rejected():
    stored_hash = hash_api_key("real-key")
    dependency = make_auth_dependency(stored_hash=stored_hash)
    with pytest.raises(HTTPException) as exc_info:
        dependency(authorization="NotBearer real-key")
    assert exc_info.value.status_code == 401
