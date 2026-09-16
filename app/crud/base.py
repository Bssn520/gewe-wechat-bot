"""条件更新：match 全中才写 fields，返回 rowcount。必须显式带 updated_at。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tortoise.models import Model


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """naive 按 UTC 墙钟补 tz；已 aware 则转到 UTC。禁止剥 tz。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def update_fields_if(
    model: type[Model], id: int, match: dict[str, Any], fields: dict[str, Any]
) -> int:
    payload = {**fields, "updated_at": utcnow()}
    filters = {k: v for k, v in match.items() if k != "id"}
    return await model.filter(id=id, **filters).update(**payload)
