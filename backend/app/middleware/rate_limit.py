import time
import uuid
from fastapi import Header, HTTPException


class SlidingWindowLimiter:
    def __init__(self, redis_client, max_requests: int, window_seconds: int):
        self._redis = redis_client
        self._max_requests = max_requests
        self._window_seconds = window_seconds

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window_start = now - self._window_seconds
        redis_key = f"ratelimit:{key}"
        self._redis.zremrangebyscore(redis_key, 0, window_start)
        current_count = self._redis.zcard(redis_key)
        if current_count >= self._max_requests:
            return False
        member = f"{now}:{uuid.uuid4().hex}"
        self._redis.zadd(redis_key, {member: now})
        self._redis.expire(redis_key, self._window_seconds)
        return True


def make_rate_limit_dependency(limiter: SlidingWindowLimiter):
    def enforce_rate_limit(authorization: str | None = Header(None)) -> None:
        if not limiter.is_allowed(authorization or "unknown"):
            raise HTTPException(status_code=429, detail="rate limit exceeded")

    return enforce_rate_limit
