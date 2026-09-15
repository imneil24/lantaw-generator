from fastapi import Header, HTTPException
from app.security import verify_api_key


def make_auth_dependency(stored_hash: str):
    def require_api_key(authorization: str | None = Header(None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing or malformed Authorization header")
        provided = authorization.removeprefix("Bearer ").strip()
        if not provided or not verify_api_key(provided, stored_hash):
            raise HTTPException(status_code=401, detail="invalid API key")
        return "primary"

    return require_api_key
