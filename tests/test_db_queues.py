from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from app.core.queue_constants import (
    INBOX_DONE,
    INBOX_FAILED,
    INBOX_PENDING,
    INBOX_PROCESSING,
    OUTBOX_PENDING,
    OUTBOX_SENDING,
    OUTBOX_SENT,
)
from app.crud import chat_session as chat_crud
from app.crud import inbox as inbox_crud
from app.crud import media as media_crud
from app.crud import node as node_crud
from app.crud import node_circuit as circuit_crud
from app.crud import outbox as outbox_crud
from app.crud.base import update_fields_if, utcnow
from app.main import app
from app.models.inbox import Inbox
from app.models.node import Node
from app.models.outbox import Outbox
from app.services import outbound as outbound_service
from app.services import reply as reply_service
from app.utils.generation import LLMError
from tests.conftest import GEWE_HEADERS, TEXT_PAYLOAD

pytestmark = pytest.mark.integration


async def _pending_inbox(
    *,
    self_wxid: str = "wx_a",
    app_id: str = "slot_a",
    friend_wxid: str = "f1",
    new_msg_id: str,
    content: str = "hi",
) -> Inbox:
    row = await inbox_crud.insert_pending(
        self_wxid=self_wxid,
        app_id=app_id,
        friend_wxid=friend_wxid,
        new_msg_id=new_msg_id,
        content=content,
    )
    assert row is not None
    return row


async def test_inbox_duplicate_new_msg_id_returns_none(db) -> None:
    first = await _pending_inbox(new_msg_id="100")
    second = await inbox_crud.insert_pending(
        self_wxid=first.self_wxid,
        app_id=first.app_id,
        friend_wxid=first.friend_wxid,
        new_msg_id="100",
        content="again",
    )
    assert second is None
    assert await Inbox.filter(self_wxid="wx_a", new_msg_id="100").count() == 1


async def test_webhook_inserts_and_dedups(db) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/gewe/callback", json=TEXT_PAYLOAD, headers=GEWE_HEADERS)
        assert resp.status_code == 200
        assert resp.content == b""
        assert await Inbox.filter(new_msg_id="91540001").count() == 1
        assert await Node.filter(self_wxid="wxid_self", current_app_id="wx_test_app").count() == 1

        resp = await client.post("/gewe/callback", json=TEXT_PAYLOAD, headers=GEWE_HEADERS)
        assert resp.status_code == 200
        assert await Inbox.filter(new_msg_id="91540001").count() == 1


async def test_claim_session_batch_takes_same_friend_only(db) -> None:
    await _pending_inbox(new_msg_id="1", content="a")
    await _pending_inbox(new_msg_id="2", content="b")
    await _pending_inbox(new_msg_id="3", content="c")
    await _pending_inbox(friend_wxid="f2", new_msg_id="4", content="other")

    peeked = await inbox_crud.peek_next_session()
    assert peeked == ("wx_a", "f1")
    batch = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f1")
    assert [row.content for row in batch] == ["a", "b", "c"]
    leftover = await Inbox.filter(friend_wxid="f2", status=INBOX_PENDING)
    assert len(leftover) == 1


async def test_peek_next_session_skips_excluded_wxid(db) -> None:
    await _pending_inbox(self_wxid="wx_a", app_id="slot_a", new_msg_id="1")
    await _pending_inbox(self_wxid="wx_b", app_id="slot_b", friend_wxid="f2", new_msg_id="2")
    assert await inbox_crud.peek_next_session() == ("wx_a", "f1")
    assert await inbox_crud.peek_next_session(exclude_wxids={"wx_a"}) == ("wx_b", "f2")
    assert await inbox_crud.peek_next_session(exclude_wxids={"wx_a", "wx_b"}) is None


async def test_peek_skips_session_already_processing(db) -> None:
    await _pending_inbox(new_msg_id="1")
    claimed = await inbox_crud.claim_session_batch(
        worker_id="w1", self_wxid="wx_a", friend_wxid="f1"
    )
    assert claimed
    await _pending_inbox(new_msg_id="2", content="later")
    await _pending_inbox(friend_wxid="f2", new_msg_id="3")
    assert await inbox_crud.peek_next_session() == ("wx_a", "f2")


async def test_peek_skips_exclude_sessions(db) -> None:
    await _pending_inbox(new_msg_id="1")
    await _pending_inbox(friend_wxid="f2", new_msg_id="2")
    assert await inbox_crud.peek_next_session(exclude_sessions={("wx_a", "f1")}) == (
        "wx_a",
        "f2",
    )


async def test_first_claim_empty_when_processing_but_debounce_still_claims(db) -> None:
    await _pending_inbox(new_msg_id="1", content="a")
    first = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f1")
    assert [row.content for row in first] == ["a"]
    await _pending_inbox(new_msg_id="2", content="b")
    assert (
        await inbox_crud.claim_session_batch(worker_id="w2", self_wxid="wx_a", friend_wxid="f1")
        == []
    )
    extra = await inbox_crud.claim_more_same_session(
        self_wxid="wx_a", friend_wxid="f1", worker_id="w1"
    )
    assert [row.content for row in extra] == ["b"]


async def test_claim_window_skips_pending_older_than_debounce(db, monkeypatch) -> None:
    monkeypatch.setattr(inbox_crud.settings, "DEBOUNCE_MS", 2000)
    first = await _pending_inbox(new_msg_id="1", content="old")
    second = await _pending_inbox(new_msg_id="2", content="new")
    await Inbox.filter(id=second.id).update(created_at=first.created_at + timedelta(seconds=5))
    batch = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f1")
    assert [row.content for row in batch] == ["old"]
    leftover = await Inbox.filter(id=second.id).first()
    assert leftover is not None and leftover.status == INBOX_PENDING


async def test_claim_sliding_window_merges_chain_longer_than_first_plus_debounce(
    db, monkeypatch
) -> None:
    """相邻都 ≤ 窗宽就并批，即使整段墙钟已经超过「首条 + DEBOUNCE」。"""
    monkeypatch.setattr(inbox_crud.settings, "DEBOUNCE_MS", 2000)
    first = await _pending_inbox(new_msg_id="1", content="a")
    second = await _pending_inbox(new_msg_id="2", content="b")
    third = await _pending_inbox(new_msg_id="3", content="c")
    await Inbox.filter(id=second.id).update(
        created_at=first.created_at + timedelta(milliseconds=1500)
    )
    await Inbox.filter(id=third.id).update(
        created_at=first.created_at + timedelta(milliseconds=3000)
    )
    batch = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f1")
    assert [row.content for row in batch] == ["a", "b", "c"]


async def test_claim_enforces_per_account_cap(db, monkeypatch) -> None:
    """认领层必须自己压住账号并发上限，不能只靠 peek。"""
    monkeypatch.setattr(inbox_crud.settings, "DEBOUNCE_MS", 0)
    monkeypatch.setattr(inbox_crud.settings, "INBOX_CONCURRENCY_PER_ACCOUNT", 2)
    for idx, friend in enumerate(("f1", "f2", "f3"), start=1):
        await _pending_inbox(new_msg_id=str(idx), friend_wxid=friend, content="hi")

    first = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f1")
    second = await inbox_crud.claim_session_batch(
        worker_id="w1", self_wxid="wx_a", friend_wxid="f2"
    )
    third = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f3")
    assert len(first) == 1 and len(second) == 1
    assert third == []
    assert (await Inbox.get(new_msg_id="3")).status == INBOX_PENDING


async def test_claim_allows_new_session_when_slot_frees(db, monkeypatch) -> None:
    monkeypatch.setattr(inbox_crud.settings, "DEBOUNCE_MS", 0)
    monkeypatch.setattr(inbox_crud.settings, "INBOX_CONCURRENCY_PER_ACCOUNT", 1)
    await _pending_inbox(new_msg_id="1", friend_wxid="f1", content="hi")
    await _pending_inbox(new_msg_id="2", friend_wxid="f2", content="hi")

    held = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f1")
    assert len(held) == 1
    assert (
        await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f2")
        == []
    )

    await inbox_crud.mark_batch_done([held[0].id], gens=[held[0].lease_gen])
    second = await inbox_crud.claim_session_batch(
        worker_id="w1", self_wxid="wx_a", friend_wxid="f2"
    )
    assert len(second) == 1


async def test_claim_more_only_takes_arrivals_within_debounce_window(db, monkeypatch) -> None:
    """补领只接「与上一条间隔 ≤ 窗宽」的链；隔太久的不得并入。"""
    monkeypatch.setattr(inbox_crud.settings, "DEBOUNCE_MS", 2000)
    first = await _pending_inbox(new_msg_id="1", content="old")
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    await conn.execute_query(
        "UPDATE inbox SET created_at = NOW() - INTERVAL '60 seconds' WHERE id = $1",
        [first.id],
    )
    batch = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f1")
    assert [row.content for row in batch] == ["old"]

    await _pending_inbox(new_msg_id="2", content="late")
    extra = await inbox_crud.claim_more_same_session(
        self_wxid="wx_a", friend_wxid="f1", worker_id="w1", after=batch[0].created_at
    )
    assert extra == []
    assert (await Inbox.get(new_msg_id="2")).status == INBOX_PENDING


async def test_claim_more_takes_arrivals_inside_debounce_window(db, monkeypatch) -> None:
    monkeypatch.setattr(inbox_crud.settings, "DEBOUNCE_MS", 2000)
    await _pending_inbox(new_msg_id="1", content="a")
    batch = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f1")
    assert [row.content for row in batch] == ["a"]

    await _pending_inbox(new_msg_id="2", content="b")
    extra = await inbox_crud.claim_more_same_session(
        self_wxid="wx_a", friend_wxid="f1", worker_id="w1", after=batch[0].created_at
    )
    assert [row.content for row in extra] == ["b"]


async def test_recent_delivered_missing_session_returns_empty(db) -> None:
    assert await chat_crud.recent_delivered(self_wxid="wx_a", friend_wxid="f1") == []


async def test_append_turn_writes_user_delivered_and_assistant_pending(db) -> None:
    await chat_crud.append_turn(
        self_wxid="wx_a", friend_wxid="f1", user_content="hi", assistant_content="yo"
    )
    messages = await chat_crud.get_messages(self_wxid="wx_a", friend_wxid="f1")
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "hi" and messages[0]["delivered"] is True
    assert messages[1]["content"] == "yo" and messages[1]["delivered"] is False
    assert messages[0]["at"]


async def test_recent_delivered_skips_undelivered_assistant(db) -> None:
    await chat_crud.append_turn(
        self_wxid="wx_a", friend_wxid="f1", user_content="hi", assistant_content="ghost"
    )
    rows = await chat_crud.recent_delivered(self_wxid="wx_a", friend_wxid="f1")
    assert [row["content"] for row in rows] == ["hi"]
    assert all(row["delivered"] for row in rows)


async def test_llm_failure_marks_inbox_failed_and_not_reclaimed(db, monkeypatch) -> None:
    monkeypatch.setattr(reply_service.settings, "DEBOUNCE_MS", 0)
    await _pending_inbox(new_msg_id="1", content="问")
    plan = SimpleNamespace(admit=AsyncMock(return_value=True))
    with (
        patch.object(reply_service, "for_account", return_value=plan),
        patch.object(
            reply_service,
            "generate_text",
            new=AsyncMock(side_effect=LLMError("llm_timeout", "slow")),
        ),
    ):
        assert await reply_service.process_one_batch("w1") is True

    failed = await Inbox.filter(status=INBOX_FAILED)
    assert len(failed) == 1
    assert failed[0].error_code == "llm_timeout"
    assert await inbox_crud.peek_next_session() is None


async def test_same_session_batch_writes_one_outbox(db, monkeypatch) -> None:
    monkeypatch.setattr(reply_service.settings, "DEBOUNCE_MS", 0)
    await _pending_inbox(new_msg_id="1", content="一")
    await _pending_inbox(new_msg_id="2", content="二")
    await _pending_inbox(new_msg_id="3", content="三")
    plan = SimpleNamespace(admit=AsyncMock(return_value=True))
    with (
        patch.object(reply_service, "for_account", return_value=plan),
        patch.object(reply_service, "generate_text", new=AsyncMock(return_value="回")),
    ):
        assert await reply_service.process_one_batch("w1") is True

    inboxes = await Inbox.all()
    assert {row.status for row in inboxes} == {INBOX_DONE}
    outboxes = await Outbox.all()
    assert len(outboxes) == 1
    assert outboxes[0].status == OUTBOX_PENDING
    assert outboxes[0].content == "回"
    assert outboxes[0].self_wxid == "wx_a"
    assert outboxes[0].app_id is None
    assert sorted(outboxes[0].source_inbox_ids) == sorted(row.id for row in inboxes)

    messages = await chat_crud.get_messages(self_wxid="wx_a", friend_wxid="f1")
    assert len(messages) == 2
    assert messages[0]["role"] == "user" and messages[0]["delivered"] is True
    assert messages[0]["content"] == "一\n二\n三"
    assert messages[1]["role"] == "assistant" and messages[1]["delivered"] is False


async def test_partial_unique_index_one_sending_per_node(db) -> None:
    await Outbox.create(
        self_wxid="wx_a",
        friend_wxid="f1",
        content="a",
        status=OUTBOX_SENDING,
    )
    with pytest.raises(IntegrityError):
        await Outbox.create(
            self_wxid="wx_a",
            friend_wxid="f2",
            content="b",
            status=OUTBOX_SENDING,
        )
    other = await Outbox.create(
        self_wxid="wx_b",
        friend_wxid="f1",
        content="c",
        status=OUTBOX_SENDING,
    )
    assert other.id is not None


async def test_peek_next_scopes_sending_lock_to_same_node(db) -> None:
    a = await outbox_crud.create_pending(
        self_wxid="wx_a", friend_wxid="f1", content="a", source_inbox_ids=[]
    )
    b = await outbox_crud.create_pending(
        self_wxid="wx_b", friend_wxid="f1", content="b", source_inbox_ids=[]
    )
    claimed = await outbox_crud.cas_claim_sending(a.id, a.lease_gen, app_id="slot_a")
    assert claimed is not None
    assert claimed.app_id == "slot_a"
    assert await outbox_crud.peek_next("wx_a") is None
    peeked_b = await outbox_crud.peek_next("wx_b")
    assert peeked_b is not None
    assert peeked_b.id == b.id


async def test_reclaim_bumps_lease_gen_and_blocks_stale_sent(db) -> None:
    row = await outbox_crud.create_pending(
        self_wxid="wx_a", friend_wxid="f1", content="hi", source_inbox_ids=[]
    )
    claimed = await outbox_crud.cas_claim_sending(row.id, row.lease_gen, app_id="slot_a")
    assert claimed is not None
    stale_gen = claimed.lease_gen

    await Outbox.filter(id=claimed.id).update(leased_until=utcnow() - timedelta(seconds=120))
    await outbox_crud.reclaim_stale()
    refreshed = await Outbox.get(id=claimed.id)
    assert refreshed.status == OUTBOX_PENDING
    assert refreshed.lease_gen == stale_gen + 1

    updated = await update_fields_if(
        Outbox,
        claimed.id,
        match={"status": OUTBOX_SENDING, "lease_gen": stale_gen},
        fields={"status": OUTBOX_SENT, "external_id": "late"},
    )
    assert updated == 0
    final = await Outbox.get(id=claimed.id)
    assert final.status == OUTBOX_PENDING
    assert final.external_id is None


async def test_schedule_retry_cas_matches_lease_gen(db) -> None:
    row = await outbox_crud.create_pending(
        self_wxid="wx_a", friend_wxid="f1", content="hi", source_inbox_ids=[]
    )
    claimed = await outbox_crud.cas_claim_sending(row.id, row.lease_gen, app_id="slot_a")
    assert claimed is not None
    assert (
        await outbox_crud.schedule_retry(
            claimed.id,
            lease_gen=claimed.lease_gen + 1,
            error_code="gewe_unavailable",
            error_message="down",
            delay_s=2.0,
        )
        == 0
    )
    stuck = await Outbox.get(id=claimed.id)
    assert stuck.status == OUTBOX_SENDING
    assert (
        await outbox_crud.schedule_retry(
            claimed.id,
            lease_gen=claimed.lease_gen,
            error_code="gewe_unavailable",
            error_message="down",
            delay_s=2.0,
        )
        == 1
    )
    retried = await Outbox.get(id=claimed.id)
    assert retried.status == OUTBOX_PENDING
    assert retried.next_retry_at is not None
    assert retried.error_code == "gewe_unavailable"


async def test_mark_sent_uses_db_now_not_before_sending_at(db) -> None:
    row = await outbox_crud.create_pending(
        self_wxid="wx_a", friend_wxid="f1", content="hi", source_inbox_ids=[]
    )
    claimed = await outbox_crud.cas_claim_sending(row.id, row.lease_gen, app_id="slot_a")
    assert claimed is not None
    n = await outbox_crud.mark_sent(claimed.id, lease_gen=claimed.lease_gen, external_id="99")
    assert n == 1
    sent = await Outbox.get(id=claimed.id)
    assert sent.status == OUTBOX_SENT
    assert sent.sent_at is not None and sent.sending_at is not None
    assert sent.sent_at >= sent.sending_at


async def test_distinct_friends_window_uses_db_now(db) -> None:
    inside = await outbox_crud.create_pending(
        self_wxid="wx_a", friend_wxid="f_in", content="in", source_inbox_ids=[]
    )
    outside = await outbox_crud.create_pending(
        self_wxid="wx_a", friend_wxid="f_out", content="out", source_inbox_ids=[]
    )
    for row in (inside, outside):
        claimed = await outbox_crud.cas_claim_sending(row.id, row.lease_gen, app_id="slot_a")
        assert claimed is not None
        n = await outbox_crud.mark_sent(claimed.id, lease_gen=claimed.lease_gen, external_id="x")
        assert n == 1
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    await conn.execute_query(
        "UPDATE outbox SET sent_at = NOW() - INTERVAL '59 seconds' WHERE id = $1",
        [inside.id],
    )
    await conn.execute_query(
        "UPDATE outbox SET sent_at = NOW() - INTERVAL '61 seconds' WHERE id = $1",
        [outside.id],
    )
    window = await outbox_crud.distinct_friends_window("wx_a")
    friends = {friend for friend, _ttl in window}
    assert "f_in" in friends
    assert "f_out" not in friends


async def test_circuit_open_survives_read(db) -> None:
    await node_crud.upsert(self_wxid="wx_a", app_id="slot_a")
    await circuit_crud.open_circuit("wx_a", reason="gewe_offline")
    assert await circuit_crud.is_open("wx_a") is True
    assert await circuit_crud.is_open("wx_b") is False


async def test_inbox_same_new_msg_id_different_nodes_ok(db) -> None:
    await _pending_inbox(self_wxid="wx_a", app_id="slot_a", new_msg_id="100")
    other = await inbox_crud.insert_pending(
        self_wxid="wx_b",
        app_id="slot_b",
        friend_wxid="f1",
        new_msg_id="100",
        content="x",
    )
    assert other is not None
    assert await Inbox.filter(new_msg_id="100").count() == 2


async def test_inbox_same_app_id_and_new_msg_id_dedups(db) -> None:
    await _pending_inbox(self_wxid="wx_a", app_id="slot_a", new_msg_id="100")
    second = await inbox_crud.insert_pending(
        self_wxid="wx_b",
        app_id="slot_a",
        friend_wxid="f1",
        new_msg_id="100",
        content="x",
    )
    assert second is None
    assert await Inbox.filter(new_msg_id="100").count() == 1


async def test_mark_last_assistant_delivered_updates_newest_undelivered_only(db) -> None:
    await chat_crud.append_turn(
        self_wxid="wx_a", friend_wxid="f1", user_content="u1", assistant_content="old"
    )
    await chat_crud.append_turn(
        self_wxid="wx_a", friend_wxid="f1", user_content="u2", assistant_content="new"
    )
    assert await chat_crud.mark_last_assistant_delivered(self_wxid="wx_a", friend_wxid="f1") == 1
    messages = await chat_crud.get_messages(self_wxid="wx_a", friend_wxid="f1")
    assistants = [m for m in messages if m["role"] == "assistant"]
    assert assistants[0]["delivered"] is False
    assert assistants[1]["delivered"] is True


async def test_mark_last_assistant_delivered_noop_when_none_undelivered(db) -> None:
    assert await chat_crud.mark_last_assistant_delivered(self_wxid="wx_a", friend_wxid="f1") == 0
    await chat_crud.append_turn(
        self_wxid="wx_a", friend_wxid="f1", user_content="u", assistant_content="a"
    )
    assert await chat_crud.mark_last_assistant_delivered(self_wxid="wx_a", friend_wxid="f1") == 1
    assert await chat_crud.mark_last_assistant_delivered(self_wxid="wx_a", friend_wxid="f1") == 0


async def test_append_turn_trims_oldest_round_when_store_window_exceeded(db, monkeypatch) -> None:
    monkeypatch.setattr(chat_crud.settings, "CHAT_STORE_ROUNDS", 2)
    await chat_crud.append_turn(
        self_wxid="wx_a", friend_wxid="f1", user_content="u1", assistant_content="a1"
    )
    await chat_crud.append_turn(
        self_wxid="wx_a", friend_wxid="f1", user_content="u2", assistant_content="a2"
    )
    await chat_crud.append_turn(
        self_wxid="wx_a", friend_wxid="f1", user_content="u3", assistant_content="a3"
    )
    messages = await chat_crud.get_messages(self_wxid="wx_a", friend_wxid="f1")
    assert [m["content"] for m in messages] == ["u2", "a2", "u3", "a3"]


async def test_recent_delivered_caps_to_reply_history_rounds(db, monkeypatch) -> None:
    monkeypatch.setattr(chat_crud.settings, "REPLY_HISTORY_ROUNDS", 1)
    for i in range(1, 4):
        await chat_crud.append_turn(
            self_wxid="wx_a",
            friend_wxid="f1",
            user_content=f"u{i}",
            assistant_content=f"a{i}",
        )
        await chat_crud.mark_last_assistant_delivered(self_wxid="wx_a", friend_wxid="f1")
    rows = await chat_crud.recent_delivered(self_wxid="wx_a", friend_wxid="f1")
    assert [row["content"] for row in rows] == ["u3", "a3"]


async def test_safe_mark_delivered_marks_undelivered_assistant(db) -> None:
    await chat_crud.append_turn(
        self_wxid="wx_a", friend_wxid="f1", user_content="u1", assistant_content="old"
    )
    await chat_crud.append_turn(
        self_wxid="wx_a", friend_wxid="f1", user_content="u2", assistant_content="new"
    )
    await outbound_service._safe_mark_delivered("wx_a", "f1")
    messages = await chat_crud.get_messages(self_wxid="wx_a", friend_wxid="f1")
    assistants = [m for m in messages if m["role"] == "assistant"]
    assert assistants[0]["delivered"] is False
    assert assistants[1]["delivered"] is True


async def test_mark_done_matches_current_lease_gen(db) -> None:
    row = await _pending_inbox(new_msg_id="cur", content="hi")
    batch = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f1")
    assert [r.id for r in batch] == [row.id]

    n = await inbox_crud.mark_batch_done([row.id], gens=[batch[0].lease_gen])
    assert n == 1
    assert (await Inbox.get(id=row.id)).status == INBOX_DONE


async def test_mark_done_rejects_stale_lease_gen(db) -> None:
    """认领 → 回收 → 他人重新认领 → 老代次回写必须 0 行。"""
    row = await _pending_inbox(new_msg_id="stale", content="hi")
    first = await inbox_crud.claim_session_batch(worker_id="A", self_wxid="wx_a", friend_wxid="f1")
    assert [r.id for r in first] == [row.id]
    stale_gen = first[0].lease_gen

    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    await conn.execute_query(
        "UPDATE inbox SET leased_until = NOW() - INTERVAL '999 seconds' WHERE id = $1",
        [row.id],
    )
    await inbox_crud.reclaim_stale()
    second = await inbox_crud.claim_session_batch(worker_id="B", self_wxid="wx_a", friend_wxid="f1")
    assert second and second[0].lease_gen == stale_gen + 2

    n = await inbox_crud.mark_batch_done([row.id], gens=[stale_gen])
    assert n == 0
    current = await Inbox.get(id=row.id)
    assert current.status == INBOX_PROCESSING
    assert current.lease_gen == second[0].lease_gen


async def test_process_session_rolls_back_when_lease_taken_over(db, monkeypatch) -> None:
    """回写行数对不上时必须整批回滚，不得留下 outbox 或历史。"""
    monkeypatch.setattr(reply_service.settings, "DEBOUNCE_MS", 0)
    await _pending_inbox(new_msg_id="rollback", content="问")
    plan = SimpleNamespace(admit=AsyncMock(return_value=True))
    with (
        patch.object(reply_service, "for_account", return_value=plan),
        patch.object(reply_service, "generate_text", new=AsyncMock(return_value="回")),
        patch.object(reply_service.inbox_crud, "mark_batch_done", new=AsyncMock(return_value=0)),
    ):
        assert await reply_service.process_one_batch("w1") is True

    assert await Outbox.all().count() == 0
    assert await chat_crud.get_messages(self_wxid="wx_a", friend_wxid="f1") == []


# --- 媒体：defer 与「XML 不泄漏」---


async def _pending_image(*, new_msg_id: str, xml: str, friend_wxid: str = "f1") -> Inbox:
    row = await inbox_crud.insert_pending(
        self_wxid="wx_a",
        app_id="slot_a",
        friend_wxid=friend_wxid,
        new_msg_id=new_msg_id,
        content=None,
        media_type="image",
        media_source={"xml": xml},
    )
    assert row is not None
    return row


IMAGE_XML = '<msg><img aeskey="k" cdnmidimgurl="f" length="100" /></msg>'


async def test_image_message_creates_media_row_and_null_content(db) -> None:
    """媒体消息 content 为 NULL，源 XML 只在 media.source。"""
    row = await _pending_image(new_msg_id="img1", xml=IMAGE_XML)
    assert row.content is None
    media = await media_crud.by_inbox_ids([row.id])
    assert len(media) == 1
    assert media[0].media_type == "image"
    assert media[0].status == "pending"
    assert media[0].source == {"xml": IMAGE_XML}


async def test_peek_defers_session_until_quiet_window_passes(db, monkeypatch) -> None:
    """媒体就绪后还要按住静默窗，把「下载期间补发的问题」收进同一批。

    静默窗走完即可认领；这里把窗宽设为 0 来模拟「已等待足够久」。
    """
    monkeypatch.setattr("app.crud.inbox.MEDIA_QUIET_MS", 0)
    await _pending_image(new_msg_id="img1", xml=IMAGE_XML)
    assert await inbox_crud.peek_next_session() is None

    media = (await media_crud.by_inbox_ids([1]))[0]
    await media_crud.mark_ready(media.id, asset_ref="http://cdn/x.png")
    peeked = await inbox_crud.peek_next_session()
    assert peeked == ("wx_a", "f1")


async def test_quiet_window_holds_session_within_window(db) -> None:
    """刚解析完的会话仍被 defer：这是静默窗的本职（默认 10 秒内不放行）。"""
    await _pending_image(new_msg_id="img1", xml=IMAGE_XML)
    media = (await media_crud.by_inbox_ids([1]))[0]
    await media_crud.mark_ready(media.id, asset_ref="http://cdn/x.png")
    # 默认 MEDIA_QUIET_MS=10000，此刻尚未走完 → 仍不认领
    assert await inbox_crud.peek_next_session() is None


async def test_peek_defers_only_that_session(db) -> None:
    """只推迟带 pending 媒体的会话，不影响其他会话（避免饿死）。"""
    await _pending_image(new_msg_id="img1", xml=IMAGE_XML)
    await _pending_inbox(new_msg_id="t1", friend_wxid="f2", content="文本")
    assert await inbox_crud.peek_next_session() == ("wx_a", "f2")


async def test_claim_window_includes_messages_arriving_during_download(db, monkeypatch) -> None:
    """核心修复：图 + 下载期间补发的问题必须收进同一批，而不是拆成两轮。

    时间线：图 t0 → 问题 t0+4 → 图就绪 t0+6.5 → 静默窗到 t0+9.5 → 一次认领收全。
    """
    monkeypatch.setattr(reply_service.settings, "DEBOUNCE_MS", 2000)
    monkeypatch.setattr("app.crud.inbox.MEDIA_QUIET_MS", 3000)

    await _pending_image(new_msg_id="img1", xml=IMAGE_XML)
    # 模拟「图还在下载」期间用户补发的问题
    await _pending_inbox(new_msg_id="q1", content="左下角什么颜色？")
    media = (await media_crud.by_inbox_ids([1]))[0]
    await media_crud.mark_ready(media.id, asset_ref="http://cdn/x.png")

    # 把时间推后到静默窗之后：媒体与 inbox 的 created_at 都提前 30 秒
    async with in_transaction() as conn:
        await conn.execute_query(
            "UPDATE media SET updated_at = NOW() - INTERVAL '30 seconds' WHERE id = $1",
            [media.id],
        )
        await conn.execute_query("UPDATE inbox SET created_at = NOW() - INTERVAL '30 seconds'")

    batch = await inbox_crud.claim_session_batch(worker_id="w1", self_wxid="wx_a", friend_wxid="f1")
    # 两条都在同一批：图与伴随问题一起进上下文
    assert [row.id for row in batch] == [1, 2]


async def test_fail_stale_media_releases_defer(db, monkeypatch) -> None:
    """媒体超时翻 failed 后必须解除 defer，否则该会话被永久推迟。"""
    monkeypatch.setattr("app.crud.inbox.MEDIA_QUIET_MS", 0)
    row = await _pending_image(new_msg_id="img1", xml=IMAGE_XML)
    assert await inbox_crud.peek_next_session() is None
    # 排队中的行（attempt=0）由第二个阈值兜底
    n = await media_crud.fail_stale(0, 0)
    assert n == 1
    media = (await media_crud.by_inbox_ids([row.id]))[0]
    assert media.status == "failed"
    assert await inbox_crud.peek_next_session() == ("wx_a", "f1")


async def test_fail_stale_spares_queued_but_kills_stuck(db) -> None:
    """区分「排队中」与「真卡死」：排队的不杀，卡死的杀。

    修的是原实现在串行下载下的误判——按 created_at 绝对年龄判死，30 张积压
    需约 200 秒，第 6 张起就在排队中被判死，有效容量被压到约 5 张。
    """
    # 排队中：attempt=0，created_at 已老（超过卡死阈值），但未超排队上限
    queued = await _pending_image(new_msg_id="img1", xml=IMAGE_XML)
    # 卡死：attempt>0 且 updated_at 已老
    stuck = await _pending_image(new_msg_id="img2", xml=IMAGE_XML)

    async with in_transaction() as conn:
        await conn.execute_query("UPDATE media SET created_at = NOW() - INTERVAL '60 seconds'")
        await conn.execute_query(
            "UPDATE media SET attempt = 1, updated_at = NOW() - INTERVAL '60 seconds' "
            "WHERE id = $1",
            [stuck.id],
        )

    # 卡死阈值 30s、排队上限 300s：排队行（60s）未超上限故存活
    n = await media_crud.fail_stale(30, 300)
    assert n == 1

    rows = {m.id: m for m in await media_crud.by_inbox_ids([queued.id, stuck.id])}
    assert rows[queued.id].status == "pending", "排队中的行不应被判死"
    assert rows[stuck.id].status == "failed", "已尝试未终态的行应被判死"


async def test_fail_stale_kills_queued_beyond_queue_timeout(db) -> None:
    """排队超绝对上限也要杀：兜底「下载循环挂掉导致永远拿不到 attempt」。"""
    row = await _pending_image(new_msg_id="img1", xml=IMAGE_XML)
    async with in_transaction() as conn:
        await conn.execute_query(
            "UPDATE media SET created_at = NOW() - INTERVAL '600 seconds' WHERE id = $1",
            [row.id],
        )
    n = await media_crud.fail_stale(30, 300)
    assert n == 1
    media = (await media_crud.by_inbox_ids([row.id]))[0]
    assert media.status == "failed"


async def test_xml_never_leaks_into_messages_or_session(db, monkeypatch) -> None:
    """最核心的保证：原始 XML 既不能进模型提示词，也不能进 chat_sessions。"""
    monkeypatch.setattr(reply_service.settings, "DEBOUNCE_MS", 0)
    # 本例只验 XML 不泄漏，与静默窗无关：置 0 让会话立即可认领
    monkeypatch.setattr("app.crud.inbox.MEDIA_QUIET_MS", 0)
    row = await _pending_image(new_msg_id="img1", xml=IMAGE_XML)
    media = (await media_crud.by_inbox_ids([row.id]))[0]
    await media_crud.mark_ready(media.id, asset_ref="http://cdn/x.png")

    plan = SimpleNamespace(admit=AsyncMock(return_value=True))
    generate = AsyncMock(return_value="看到了")
    with (
        patch.object(reply_service, "for_account", return_value=plan),
        patch.object(reply_service, "generate_text", new=generate),
    ):
        assert await reply_service.process_one_batch("w1") is True

    sent_messages = generate.await_args.kwargs["messages"]
    blob = str(sent_messages) + str(generate.await_args.kwargs.get("images"))
    assert "cdnmidimgurl" not in blob
    assert "<msg>" not in blob
    assert "[图片]" in blob  # 是占位，不是 XML

    stored = await chat_crud.get_messages(self_wxid="wx_a", friend_wxid="f1")
    stored_blob = str(stored)
    assert "cdnmidimgurl" not in stored_blob
    assert "<msg>" not in stored_blob
    assert stored[0]["content"] == "[图片]"
