from __future__ import annotations

from app.core.rate_limit.intent import SendIntent
from app.crud import node_circuit as circuit_crud


async def admit(intent: SendIntent) -> bool:
    return not await circuit_crud.is_open(intent.self_wxid)
