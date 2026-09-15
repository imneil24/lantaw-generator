import time
from app.middleware.rate_limit import SlidingWindowLimiter


class FakeRedis:
    def __init__(self):
        self._store = {}

    def zremrangebyscore(self, key, min_score, max_score):
        self._store.setdefault(key, {})
        self._store[key] = {
            member: score for member, score in self._store[key].items()
            if not (min_score <= score <= max_score)
        }

    def zcard(self, key):
        return len(self._store.get(key, {}))

    def zadd(self, key, mapping):
        self._store.setdefault(key, {})
        self._store[key].update(mapping)

    def expire(self, key, seconds):
        pass


def test_allows_requests_under_limit():
    limiter = SlidingWindowLimiter(FakeRedis(), max_requests=3, window_seconds=60)
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is True
    assert limiter.is_allowed("client1") is True


def test_blocks_requests_over_limit():
    limiter = SlidingWindowLimiter(FakeRedis(), max_requests=2, window_seconds=60)
    assert limiter.is_allowed("client2") is True
    assert limiter.is_allowed("client2") is True
    assert limiter.is_allowed("client2") is False


def test_limits_are_per_key():
    redis = FakeRedis()
    limiter = SlidingWindowLimiter(redis, max_requests=1, window_seconds=60)
    assert limiter.is_allowed("clientA") is True
    assert limiter.is_allowed("clientB") is True
