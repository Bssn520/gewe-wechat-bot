from __future__ import annotations

from enum import StrEnum

import structlog
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from app.crud.base import utcnow
from app.models.node import Node

logger = structlog.get_logger(__name__)


class UpsertOutcome(StrEnum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    ROTATED = "rotated"
    REJECTED = "rejected"


async def get_app_id(self_wxid: str) -> str | None:
    row = await Node.get_or_none(self_wxid=self_wxid)
    return row.current_app_id if row else None


async def get_wxid(app_id: str) -> str | None:
    row = await Node.get_or_none(current_app_id=app_id)
    return row.self_wxid if row else None


async def list_wxids() -> list[str]:
    rows = await Node.all().order_by("id")
    return [row.self_wxid for row in rows]


async def upsert(*, self_wxid: str, app_id: str) -> UpsertOutcome:
    """同一人换槽则覆盖；槽已被别人占用则拒绝改绑。

    先用单条 ``INSERT ... ON CONFLICT DO NOTHING`` 原子占位，再在干净语句里判读：
    并发首见同一 ``self_wxid`` 时不再抛 ``IntegrityError``，也就不会在已中止的事务里
    继续查询（那会连锁成 ``TransactionManagementError`` 并把连接弄脏）。
    """
    now = utcnow()
    async with in_transaction() as conn:
        inserted = await conn.execute_query_dict(
            """
            INSERT INTO nodes (self_wxid, current_app_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            [self_wxid, app_id, now, now],
        )

    by_wxid = await Node.get_or_none(self_wxid=self_wxid)
    if by_wxid is None:
        # 占位没落下来，只可能是 current_app_id 撞上了别人的行。
        owner = await Node.get_or_none(current_app_id=app_id)
        logger.error(
            "node.upsert.app_id_bound",
            self_wxid=self_wxid,
            app_id=app_id,
            owner=owner.self_wxid if owner else None,
        )
        return UpsertOutcome.REJECTED

    if by_wxid.current_app_id == app_id:
        return UpsertOutcome.CREATED if inserted else UpsertOutcome.UNCHANGED

    old = by_wxid.current_app_id
    try:
        async with in_transaction() as conn:
            result = await conn.execute_query(
                """
                UPDATE nodes
                SET current_app_id = $2, updated_at = $3
                WHERE self_wxid = $1 AND current_app_id <> $2
                """,
                [self_wxid, app_id, utcnow()],
            )
    except IntegrityError:
        logger.error("node.upsert.app_id_bound", self_wxid=self_wxid, app_id=app_id)
        return UpsertOutcome.REJECTED
    if int(result[0] or 0) == 0:
        return UpsertOutcome.UNCHANGED
    logger.warning("node.slot_rotated", self_wxid=self_wxid, old_app_id=old, app_id=app_id)
    return UpsertOutcome.ROTATED
