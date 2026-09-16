from __future__ import annotations

from app.core.rate_limit.gates import (
    backlog,
    circuit,
    daily_quota,
    distinct_friends,
    interval,
    token_bucket,
)
from app.core.rate_limit.intent import SendIntent


class RatePlan:
    async def admit(self, intent: SendIntent) -> bool:
        if not await circuit.admit(intent):
            return False
        if not await daily_quota.admit(intent):
            return False
        return await backlog.admit(intent)

    async def circuit_ok(self, intent: SendIntent) -> bool:
        """发送前/等待后：熔断开着就不能 postText。不碰令牌桶。"""
        return await circuit.admit(intent)

    async def wait_to_dispatch(self, intent: SendIntent) -> float:
        d1 = await distinct_friends.delay_seconds(intent)
        d2 = token_bucket.delay_seconds(intent)
        d3 = await interval.delay_seconds(intent)
        return max(d1, d2, d3)
