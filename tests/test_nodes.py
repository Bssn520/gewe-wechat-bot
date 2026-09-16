from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.crud import node as node_crud
from app.crud import node_circuit as circuit_crud
from app.crud.base import utcnow
from app.crud.node import UpsertOutcome
from app.models.node import Node
from app.workers.reply import missing_send_loops

pytestmark = pytest.mark.integration


async def test_upsert_creates_and_is_idempotent(db) -> None:
    assert await node_crud.upsert(self_wxid="wx_a", app_id="slot_1") is UpsertOutcome.CREATED
    assert await node_crud.upsert(self_wxid="wx_a", app_id="slot_1") is UpsertOutcome.UNCHANGED
    assert await node_crud.get_app_id("wx_a") == "slot_1"
    assert await node_crud.get_wxid("slot_1") == "wx_a"
    assert await node_crud.list_wxids() == ["wx_a"]


async def test_upsert_rotates_slot_without_clearing_circuit(db) -> None:
    await node_crud.upsert(self_wxid="wx_a", app_id="slot_1")
    await circuit_crud.open_circuit("wx_a", reason="gewe_offline")
    assert await node_crud.upsert(self_wxid="wx_a", app_id="slot_2") is UpsertOutcome.ROTATED
    row = await Node.get(self_wxid="wx_a")
    assert row.current_app_id == "slot_2"
    assert row.circuit_reason == "gewe_offline"
    assert await circuit_crud.is_open("wx_a") is True


async def test_upsert_rejects_app_id_bound_to_other_wxid(db) -> None:
    await node_crud.upsert(self_wxid="wx_a", app_id="slot_1")
    assert await node_crud.upsert(self_wxid="wx_b", app_id="slot_1") is UpsertOutcome.REJECTED
    assert await Node.filter(self_wxid="wx_b").count() == 0


async def test_upsert_concurrent_same_pair_does_not_reject(db) -> None:
    outcomes = await asyncio.gather(
        *[node_crud.upsert(self_wxid="wx_a", app_id="slot_1") for _ in range(8)]
    )
    assert UpsertOutcome.REJECTED not in outcomes
    assert set(outcomes) <= {UpsertOutcome.CREATED, UpsertOutcome.UNCHANGED}
    assert outcomes.count(UpsertOutcome.CREATED) == 1
    assert await Node.filter(self_wxid="wx_a").count() == 1
    assert await node_crud.get_app_id("wx_a") == "slot_1"


async def test_upsert_survives_row_committed_by_concurrent_txn(db) -> None:
    """确定性复现压测里的竞态：另一个事务先占住同一 self_wxid（未提交），upsert 必须扛住。

    时序：独立连接插入 wx_a 但先不提交 → upsert 读不到（未提交不可见）→ 走到写 →
    对端提交 → 唯一冲突。旧实现在冲突后仍于同一已中止事务里查询，抛
    TransactionManagementError（整条回调被丢）；新实现应返回 UNCHANGED 且不抛。
    """
    import asyncpg

    from app.core.config import settings

    other = await asyncpg.connect(settings.DATABASE_URL)
    tx = other.transaction()
    await tx.start()
    await other.execute(
        "INSERT INTO nodes (self_wxid, current_app_id, created_at, updated_at) "
        "VALUES ($1, $2, NOW(), NOW())",
        "wx_a",
        "slot_1",
    )

    async def _commit_soon() -> None:
        await asyncio.sleep(0.3)  # 让 upsert 先完成读、走到写并阻塞在唯一索引上
        await tx.commit()

    task = asyncio.create_task(node_crud.upsert(self_wxid="wx_a", app_id="slot_1"))
    await _commit_soon()
    try:
        outcome = await asyncio.wait_for(task, timeout=10)
    finally:
        await other.close()

    assert outcome is UpsertOutcome.UNCHANGED
    assert await Node.filter(self_wxid="wx_a").count() == 1
    assert await node_crud.get_app_id("wx_a") == "slot_1"


async def test_open_circuit_requires_existing_node(db) -> None:
    assert await circuit_crud.open_circuit("wx_missing", reason="gewe_offline") is False


async def test_close_if_expired_only_clears_due_rows(db) -> None:
    await node_crud.upsert(self_wxid="wx_a", app_id="slot_1")
    await circuit_crud.open_circuit("wx_a", reason="gewe_risk", seconds=60)
    await circuit_crud.close_if_expired("wx_a")
    assert await circuit_crud.is_open("wx_a") is True

    row = await Node.get(self_wxid="wx_a")
    row.circuit_open_until = utcnow() - timedelta(seconds=1)
    await row.save(update_fields=["circuit_open_until"])
    await circuit_crud.close_if_expired("wx_a")
    refreshed = await Node.get(self_wxid="wx_a")
    assert refreshed.circuit_open_until is None
    assert refreshed.circuit_reason is None
    assert await circuit_crud.is_open("wx_a") is False


def test_missing_send_loops() -> None:
    assert missing_send_loops(["a", "b"], {"a"}) == ["b"]
    assert missing_send_loops(["a"], {"a", "b"}) == []
