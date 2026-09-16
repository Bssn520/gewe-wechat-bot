from __future__ import annotations

from datetime import datetime, timedelta

from app.crud.base import as_utc, utcnow
from app.models.node import Node

DEFAULT_CIRCUIT_SECONDS = 30 * 60


async def is_open(self_wxid: str, now: datetime | None = None) -> bool:
    row = await Node.get_or_none(self_wxid=self_wxid)
    if row is None or row.circuit_open_until is None:
        return False
    stamp = as_utc(now) if now is not None else utcnow()
    return as_utc(row.circuit_open_until) > stamp


async def open_circuit(
    self_wxid: str, *, reason: str, seconds: int = DEFAULT_CIRCUIT_SECONDS
) -> bool:
    """命中已有节点才熔断。没有行则不建节点。"""
    row = await Node.get_or_none(self_wxid=self_wxid)
    if row is None:
        return False
    row.circuit_open_until = utcnow() + timedelta(seconds=seconds)
    row.circuit_reason = reason[:64]
    row.updated_at = utcnow()
    await row.save(update_fields=["circuit_open_until", "circuit_reason", "updated_at"])
    return True


async def close_if_expired(self_wxid: str) -> None:
    row = await Node.get_or_none(self_wxid=self_wxid)
    if row is None or row.circuit_open_until is None:
        return
    if as_utc(row.circuit_open_until) <= utcnow():
        row.circuit_open_until = None
        row.circuit_reason = None
        row.updated_at = utcnow()
        await row.save(update_fields=["circuit_open_until", "circuit_reason", "updated_at"])
