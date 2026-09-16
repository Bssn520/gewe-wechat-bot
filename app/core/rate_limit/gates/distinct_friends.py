from __future__ import annotations

from app.core.config import settings
from app.core.rate_limit.intent import SendIntent
from app.crud import outbox as outbox_crud


async def delay_seconds(intent: SendIntent) -> float:
    """已在窗口内立刻放行；新好友且集合已满则等到最早过期的那位滑出。"""
    window = await outbox_crud.distinct_friends_window(intent.self_wxid)
    friends = {friend for friend, _ttl in window}
    if intent.friend_wxid in friends:
        return 0.0
    if len(friends) < settings.SEND_MAX_DISTINCT_FRIENDS_PER_MINUTE:
        return 0.0
    return max(0.0, min(ttl for _friend, ttl in window))
