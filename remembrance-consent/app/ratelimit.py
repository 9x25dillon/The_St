"""Rate limiting with GCRA (generic cell rate algorithm).

GCRA keeps one number per key, the theoretical arrival time (TAT):

    T   = period / rate            emission interval
    tau = T * (burst - 1)          tolerance: `burst` requests may arrive at once
    tat = max(stored_tat, now)
    allow iff now >= tat - tau;    on allow, stored_tat = tat + T

It is exact (no window-boundary double bursts), O(1) time and state per
key, and the same arithmetic runs in-process or atomically in Redis.
"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import ApiError, error_response


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: float = 0.0


class RateLimiter(Protocol):
    def hit(self, key: str) -> RateDecision: ...


def _parameters(rate_per_minute: int, burst: int) -> tuple[float, float]:
    if rate_per_minute < 1 or burst < 1:
        raise ValueError("rate and burst must be positive")
    interval = 60.0 / rate_per_minute
    return interval, interval * (burst - 1)


class InMemoryGCRA:
    """Per-process limiter. With several workers, each enforces its own
    budget; use RedisGCRA for a shared one."""

    MAX_KEYS = 100_000

    def __init__(self, rate_per_minute: int, burst: int, now: Callable[[], float] = time.monotonic) -> None:
        self._interval, self._tolerance = _parameters(rate_per_minute, burst)
        self._now = now
        self._tat: dict[str, float] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> RateDecision:
        with self._lock:
            now = self._now()
            tat = max(self._tat.get(key, now), now)
            allow_at = tat - self._tolerance
            if now < allow_at:
                return RateDecision(False, allow_at - now)
            self._tat[key] = tat + self._interval
            if len(self._tat) > self.MAX_KEYS:
                self._tat = {k: v for k, v in self._tat.items() if v > now}
            return RateDecision(True)


_REDIS_GCRA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local interval = tonumber(ARGV[1])
local tolerance = tonumber(ARGV[2])
local tat = tonumber(redis.call('GET', KEYS[1])) or now
if tat < now then tat = now end
local allow_at = tat - tolerance
if now < allow_at then
  return {0, tostring(allow_at - now)}
end
local new_tat = tat + interval
redis.call('SET', KEYS[1], tostring(new_tat), 'PX', math.ceil((new_tat - now) * 1000) + 1000)
return {1, '0'}
"""


class RedisGCRA:
    """Shared limiter: one atomic Lua script, Redis server time as the clock."""

    def __init__(self, client, rate_per_minute: int, burst: int, prefix: str = "remembrance:rl:") -> None:
        self._interval, self._tolerance = _parameters(rate_per_minute, burst)
        self._script = client.register_script(_REDIS_GCRA)
        self._prefix = prefix

    def hit(self, key: str) -> RateDecision:
        allowed, retry_after = self._script(keys=[self._prefix + key], args=[self._interval, self._tolerance])
        return RateDecision(bool(int(allowed)), float(retry_after))


class RateLimitMiddleware:
    """Limits POSTs to the given paths, keyed by the authenticated actor.
    Sits inside AuthMiddleware and outside the purpose guard, so a flood of
    banned-purpose requests is throttled before it can flood the audit log."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter, paths: Iterable[str]) -> None:
        self.app = app
        self.limiter = limiter
        self.paths = frozenset(paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in self.paths:
            await self.app(scope, receive, send)
            return
        actor = scope.get("state", {}).get("actor")
        client = scope.get("client") or ("unknown", 0)
        key = f"actor:{actor.user_id}" if actor is not None else f"ip:{client[0]}"
        decision = self.limiter.hit(f"{scope['path']}:{key}")
        if not decision.allowed:
            retry = max(1, math.ceil(decision.retry_after_seconds))
            response = error_response(
                ApiError(429, "RATE_LIMITED", "too many authorization requests", retry_after_seconds=retry),
                headers={"Retry-After": str(retry)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
