from app.security import hash_api_key, verify_api_key


def test_hash_then_verify_succeeds():
    raw = "test-api-key-12345"
    hashed = hash_api_key(raw)
    assert verify_api_key(raw, hashed) is True


def test_verify_rejects_wrong_key():
    hashed = hash_api_key("correct-key")
    assert verify_api_key("wrong-key", hashed) is False


def test_hash_is_not_plaintext():
    raw = "test-api-key-12345"
    hashed = hash_api_key(raw)
    assert raw not in hashed
