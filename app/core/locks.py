"""PostgreSQL advisory lock。

约定：
- 用 try 不阻塞；抢不到就跳过/退出。
- acquire / unlock 必须 pin 同一底层连接。
- 非 PG 直接放行，且不 pin（避免 SQLite 死锁）。
- 持锁期间 pin 占 1 个池连接；业务 SQL 走池。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import structlog
from tortoise import connections

logger = structlog.get_logger(__name__)

# 禁止文本键。88440101 = worker 全局单实例。
ADVISORY_LOCK_WORKER: Final[tuple[int, int]] = (88440101, 1)


def _is_postgres_backend(obj: Any) -> bool:
    name = type(obj).__name__.lower()
    module = type(obj).__module__.lower()
    return "asyncpg" in module or "postgres" in name or "postgres" in module or "asyncpg" in name


def _client_is_postgres(client: Any) -> bool:
    return _is_postgres_backend(client)


def _connection_is_postgres(conn: Any) -> bool:
    return _is_postgres_backend(conn)


async def try_advisory_lock(conn: Any, key1: int, key2: int) -> bool:
    if not _connection_is_postgres(conn):
        return True
    row = await conn.fetchrow("SELECT pg_try_advisory_lock($1, $2) AS ok", key1, key2)
    if row is None:
        return False
    return bool(row["ok"])


async def advisory_unlock(conn: Any, key1: int, key2: int) -> None:
    if not _connection_is_postgres(conn):
        return
    await conn.execute("SELECT pg_advisory_unlock($1, $2)", key1, key2)


@asynccontextmanager
async def pinned_connection() -> AsyncIterator[Any]:
    client = connections.get("default")
    async with client.acquire_connection() as raw_conn:
        yield raw_conn


@asynccontextmanager
async def worker_single_instance_lock() -> AsyncIterator[bool]:
    """同一时刻只有一个 worker 在发送。Yields True 若拿到锁（或非 PG）。"""
    key1, key2 = ADVISORY_LOCK_WORKER
    client = connections.get("default")
    if not _client_is_postgres(client):
        yield True
        return

    async with pinned_connection() as conn:
        ok = await try_advisory_lock(conn, key1, key2)
        if not ok:
            logger.error("worker.lock.busy", key1=key1, key2=key2)
            yield False
            return
        try:
            yield True
        finally:
            try:
                await advisory_unlock(conn, key1, key2)
            except Exception:
                logger.warning("worker.lock.unlock_failed", exc_info=True)
