from __future__ import annotations

import json
from typing import Any

from tortoise.transactions import in_transaction

from app.core.config import settings
from app.core.queue_constants import OUTBOX_FAILED, OUTBOX_PENDING, OUTBOX_SENDING, OUTBOX_SENT
from app.crud.base import update_fields_if
from app.models.outbox import Outbox


async def create_pending(
    *,
    self_wxid: str,
    friend_wxid: str,
    content: str,
    source_inbox_ids: list[int],
) -> Outbox:
    """created_at / updated_at 走 DB NOW()，与 sending_at / sent_at 同源，避免行内混钟。"""
    async with in_transaction() as conn:
        rows = await conn.execute_query_dict(
            """
            INSERT INTO outbox (
                self_wxid, friend_wxid, content, source_inbox_ids,
                status, attempt, lease_gen, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4::jsonb, $5, 0, 0, NOW(), NOW())
            RETURNING id
            """,
            [self_wxid, friend_wxid, content, json.dumps(source_inbox_ids), OUTBOX_PENDING],
        )
    return await Outbox.get(id=rows[0]["id"])


async def pending_count(self_wxid: str) -> int:
    return await Outbox.filter(self_wxid=self_wxid, status=OUTBOX_PENDING).count()


async def sent_count_today(self_wxid: str) -> int:
    tz = settings.QUOTA_TZ
    async with in_transaction() as conn:
        rows = await conn.execute_query_dict(
            """
            SELECT COUNT(*) AS n FROM outbox
            WHERE self_wxid = $1 AND status = $2 AND sent_at IS NOT NULL
              AND (sent_at AT TIME ZONE $3)::date
                  = (NOW() AT TIME ZONE $3)::date
            """,
            [self_wxid, OUTBOX_SENT, tz],
        )
    return int(rows[0]["n"] if rows else 0)


async def distinct_friends_window(self_wxid: str) -> list[tuple[str, float]]:
    """最近 60s 内出现过的好友，及距其滑出窗口还剩几秒（按该好友最后一次 sent）。"""
    async with in_transaction() as conn:
        rows = await conn.execute_query_dict(
            """
            SELECT friend_wxid,
                   EXTRACT(EPOCH FROM (MAX(sent_at) + INTERVAL '60 seconds' - NOW())) AS ttl_s
            FROM outbox
            WHERE self_wxid = $1 AND status = $2 AND sent_at IS NOT NULL
              AND sent_at > NOW() - INTERVAL '60 seconds'
            GROUP BY friend_wxid
            """,
            [self_wxid, OUTBOX_SENT],
        )
    return [(row["friend_wxid"], float(row["ttl_s"] or 0.0)) for row in rows]


async def last_sent(self_wxid: str) -> tuple[str, float] | None:
    """最近一条 sent：好友，以及距今几秒（两端都走 DB NOW()）。"""
    async with in_transaction() as conn:
        rows = await conn.execute_query_dict(
            """
            SELECT friend_wxid,
                   EXTRACT(EPOCH FROM (NOW() - sent_at)) AS elapsed_s
            FROM outbox
            WHERE self_wxid = $1 AND status = $2 AND sent_at IS NOT NULL
            ORDER BY sent_at DESC
            LIMIT 1
            """,
            [self_wxid, OUTBOX_SENT],
        )
    if not rows:
        return None
    return rows[0]["friend_wxid"], float(rows[0]["elapsed_s"] or 0.0)


async def peek_next(self_wxid: str) -> Outbox | None:
    async with in_transaction() as conn:
        rows = await conn.execute_query_dict(
            """
            SELECT o.id
            FROM outbox o
            WHERE o.status = $1 AND o.self_wxid = $2
              AND (o.next_retry_at IS NULL OR o.next_retry_at <= NOW())
              AND NOT EXISTS (
                SELECT 1 FROM outbox x
                WHERE x.self_wxid = o.self_wxid AND x.status = $3
              )
            ORDER BY o.id
            LIMIT 1
            """,
            [OUTBOX_PENDING, self_wxid, OUTBOX_SENDING],
        )
    if not rows:
        return None
    return await Outbox.get_or_none(id=rows[0]["id"])


async def cas_claim_sending(outbox_id: int, peeked_gen: int, *, app_id: str) -> Outbox | None:
    lease = settings.SEND_LEASE_SECONDS
    async with in_transaction() as conn:
        rows = await conn.execute_query_dict(
            """
            UPDATE outbox
            SET status = $1,
                sending_at = COALESCE(sending_at, NOW()),
                leased_until = NOW() + ($2 * INTERVAL '1 second'),
                lease_gen = lease_gen + 1,
                attempt = attempt + 1,
                app_id = $6,
                updated_at = NOW()
            WHERE id = $3 AND status = $4 AND lease_gen = $5
            RETURNING id
            """,
            [OUTBOX_SENDING, lease, outbox_id, OUTBOX_PENDING, peeked_gen, app_id],
        )
    if not rows:
        return None
    return await Outbox.get_or_none(id=rows[0]["id"])


async def mark_sent(outbox_id: int, *, lease_gen: int, external_id: str | None) -> int:
    """CAS 标 sent；sent_at / updated_at 走 DB NOW()。"""
    async with in_transaction() as conn:
        result = await conn.execute_query(
            """
            UPDATE outbox
            SET status = $1,
                sent_at = NOW(),
                external_id = $2,
                error_code = NULL,
                error_message = NULL,
                leased_until = NULL,
                updated_at = NOW()
            WHERE id = $3 AND status = $4 AND lease_gen = $5
            """,
            [OUTBOX_SENT, external_id, outbox_id, OUTBOX_SENDING, lease_gen],
        )
    return int(result[0] or 0)


async def schedule_retry(
    outbox_id: int,
    *,
    lease_gen: int,
    error_code: str,
    error_message: str,
    delay_s: float,
) -> int:
    async with in_transaction() as conn:
        result = await conn.execute_query(
            """
            UPDATE outbox
            SET status = $1,
                leased_until = NULL,
                error_code = $2,
                error_message = $3,
                next_retry_at = NOW() + ($4 * INTERVAL '1 second'),
                updated_at = NOW()
            WHERE id = $5 AND status = $6 AND lease_gen = $7
            """,
            [
                OUTBOX_PENDING,
                error_code,
                error_message[:500],
                delay_s,
                outbox_id,
                OUTBOX_SENDING,
                lease_gen,
            ],
        )
    return int(result[0] or 0)


async def mark_failed(
    outbox_id: int,
    *,
    match: dict[str, Any],
    error_code: str,
    error_message: str,
) -> int:
    return await update_fields_if(
        Outbox,
        outbox_id,
        match=match,
        fields={
            "status": OUTBOX_FAILED,
            "leased_until": None,
            "error_code": error_code,
            "error_message": error_message[:500],
        },
    )


async def reclaim_stale() -> int:
    grace = settings.SEND_LEASE_GRACE_SECONDS
    async with in_transaction() as conn:
        result = await conn.execute_query(
            """
            UPDATE outbox
            SET status = $1,
                leased_until = NULL,
                lease_gen = lease_gen + 1,
                updated_at = NOW()
            WHERE status = $2
              AND leased_until IS NOT NULL
              AND leased_until + ($3 * INTERVAL '1 second') < NOW()
            """,
            [OUTBOX_PENDING, OUTBOX_SENDING, grace],
        )
    return int(result[0] or 0)
