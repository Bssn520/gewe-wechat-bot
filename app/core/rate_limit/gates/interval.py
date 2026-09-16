from __future__ import annotations

import random

from app.core.config import settings
from app.core.rate_limit.intent import SendIntent
from app.crud import outbox as outbox_crud


async def delay_seconds(intent: SendIntent) -> float:
    last = await outbox_crud.last_sent(intent.self_wxid)
    if last is None:
        return 0.0
    last_friend, elapsed = last
    same = last_friend == intent.friend_wxid
    low, high = settings.send_interval_same_s if same else settings.send_interval_diff_s
    need = random.uniform(low, high)
    return max(0.0, need - elapsed)
