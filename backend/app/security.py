import bcrypt


def hash_api_key(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_api_key(provided: str, stored_hash: str) -> bool:
    try:
        return bcrypt.checkpw(provided.encode("utf-8"), stored_hash.encode("utf-8"))
    except ValueError:
        return False
