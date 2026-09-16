from __future__ import annotations

import json
from typing import Any

from tortoise.transactions import in_transaction

from app.core.config import settings
from app.core.queue_constants import ROLE_ASSISTANT, ROLE_USER
from app.crud.base import utcnow
from app.models.chat_session import ChatSession


def _as_message(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return value


def _as_messages(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return [_as_message(item) for item in value]


def delivered_window(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """倒序取出的元素 → 时间正序、仅 delivered、截到模型窗口。"""
    limit = settings.REPLY_HISTORY_ROUNDS * 2
    kept = [e for e in reversed(elements) if e.get("delivered") is True]
    if len(kept) > limit:
        kept = kept[-limit:]
    return kept


def _pair(*, user_content: str, assistant_content: str) -> list[dict[str, Any]]:
    stamp = utcnow().isoformat()
    return [
        {
            "role": ROLE_USER,
            "content": user_content,
            "delivered": True,
            "at": stamp,
        },
        {
            "role": ROLE_ASSISTANT,
            "content": assistant_content,
            "delivered": False,
            "at": stamp,
        },
    ]


async def recent_delivered(*, self_wxid: str, friend_wxid: str) -> list[dict[str, Any]]:
    """SQL 倒序 LIMIT，不把整列 messages 拉进 Python。"""
    tail = settings.REPLY_HISTORY_ROUNDS * 2 + 2
    async with in_transaction() as conn:
        rows = await conn.execute_query_dict(
            """
            SELECT e
            FROM chat_sessions,
                 jsonb_array_elements(messages) WITH ORDINALITY AS t(e, n)
            WHERE self_wxid = $1 AND friend_wxid = $2
            ORDER BY n DESC
            LIMIT $3
            """,
            [self_wxid, friend_wxid, tail],
        )
    elements = [_as_message(row["e"]) for row in rows]
    return delivered_window(elements)


async def append_turn(
    *,
    self_wxid: str,
    friend_wxid: str,
    user_content: str,
    assistant_content: str,
) -> None:
    """SQL 拼接一对消息；超存储窗口则从队头砍整轮。"""
    pair = json.dumps(_pair(user_content=user_content, assistant_content=assistant_content))
    cap = settings.CHAT_STORE_ROUNDS * 2
    async with in_transaction() as conn:
        await conn.execute_query(
            """
            INSERT INTO chat_sessions (self_wxid, friend_wxid, messages, created_at, updated_at)
            VALUES ($1, $2, $3::jsonb, NOW(), NOW())
            ON CONFLICT (self_wxid, friend_wxid) DO UPDATE SET
                messages = (
                    WITH grown AS (
                        SELECT COALESCE(chat_sessions.messages, '[]'::jsonb) || $3::jsonb AS blob
                    ),
                    numbered AS (
                        SELECT e, n, jsonb_array_length(grown.blob) AS len
                        FROM grown,
                             jsonb_array_elements(grown.blob) WITH ORDINALITY AS t(e, n)
                    )
                    SELECT COALESCE(jsonb_agg(e ORDER BY n), '[]'::jsonb)
                    FROM numbered
                    WHERE n > GREATEST(len - $4, 0)
                ),
                updated_at = NOW()
            """,
            [self_wxid, friend_wxid, pair, cap],
        )


async def mark_last_assistant_delivered(*, self_wxid: str, friend_wxid: str) -> int:
    """从后往前第一条未送达 assistant 改为 true。无行或没有未送达则 0。"""
    async with in_transaction() as conn:
        rows = await conn.execute_query_dict(
            """
            SELECT id, messages FROM chat_sessions
            WHERE self_wxid = $1 AND friend_wxid = $2
            FOR UPDATE
            """,
            [self_wxid, friend_wxid],
        )
        if not rows:
            return 0
        messages = _as_messages(rows[0]["messages"])
        for i in range(len(messages) - 1, -1, -1):
            item = messages[i]
            if item.get("role") == ROLE_ASSISTANT and item.get("delivered") is False:
                messages[i] = {**item, "delivered": True}
                await conn.execute_query(
                    """
                    UPDATE chat_sessions
                    SET messages = $1::jsonb, updated_at = NOW()
                    WHERE id = $2
                    """,
                    [json.dumps(messages), rows[0]["id"]],
                )
                return 1
        return 0


async def get_messages(*, self_wxid: str, friend_wxid: str) -> list[dict[str, Any]]:
    """测试/排障读整列；业务热路径不要用。"""
    row = await ChatSession.get_or_none(self_wxid=self_wxid, friend_wxid=friend_wxid)
    if row is None:
        return []
    return _as_messages(row.messages)
