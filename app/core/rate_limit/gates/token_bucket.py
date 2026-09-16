"""手写令牌桶。容量 = SEND_BURST，速率 = SEND_MAX_PER_MINUTE。自己不 sleep。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.config import settings
from app.core.rate_limit.intent import SendIntent


@dataclass
class _Bucket:
    tokens: float
    last: float


_buckets: dict[str, _Bucket] = {}


def _rate_per_sec() -> float:
    return settings.SEND_MAX_PER_MINUTE / 60.0


def _refill(bucket: _Bucket, now: float) -> None:
    cap = float(settings.SEND_BURST)
    rate = _rate_per_sec()
    elapsed = max(0.0, now - bucket.last)
    bucket.tokens = min(cap, bucket.tokens + elapsed * rate)
    bucket.last = now


def delay_seconds(intent: SendIntent) -> float:
    now = time.monotonic()
    bucket = _buckets.get(intent.self_wxid)
    if bucket is None:
        bucket = _Bucket(tokens=float(settings.SEND_BURST), last=now)
        _buckets[intent.self_wxid] = bucket
    _refill(bucket, now)
    bucket.tokens -= 1.0
    if bucket.tokens >= 0.0:
        return 0.0
    wait = (-bucket.tokens) / _rate_per_sec()
    return wait
