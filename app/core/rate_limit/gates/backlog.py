from __future__ import annotations

from app.core.config import settings
from app.core.rate_limit.intent import SendIntent
from app.crud import outbox as outbox_crud


async def admit(intent: SendIntent) -> bool:
    n = await outbox_crud.pending_count(intent.self_wxid)
    return n < settings.OUTBOX_BACKLOG_PER_ACCOUNT
