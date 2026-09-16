from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.gewe_errors import GeWeResult
from app.services import outbound as outbound_service


def _row(**kwargs) -> SimpleNamespace:
    data = {
        "id": 9,
        "self_wxid": "wx_a",
        "app_id": "slot_a",
        "friend_wxid": "f1",
        "content": "hi",
        "lease_gen": 3,
        "attempt": 1,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


def test_typing_delay_clamped() -> None:
    assert outbound_service.typing_delay_s("") == 0.8
    assert outbound_service.typing_delay_s("a" * 50) == pytest.approx(2.0)
    assert outbound_service.typing_delay_s("a" * 1000) == 5.0


@pytest.fixture
def no_typing(monkeypatch) -> None:
    monkeypatch.setattr(outbound_service, "typing_delay_s", lambda text: 0.0)


def _slot(app_id: str | None = "slot_a") -> object:
    return patch.object(
        outbound_service.node_crud, "get_app_id", new=AsyncMock(return_value=app_id)
    )


async def test_process_one_empty_peek_returns_false(no_typing) -> None:
    with patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=None)):
        assert await outbound_service.process_one("wx_a") is False


def _plan(*, delay: float = 0.0, circuit: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        wait_to_dispatch=AsyncMock(return_value=delay),
        circuit_ok=AsyncMock(return_value=circuit),
    )


async def test_missing_slot_does_not_send(no_typing) -> None:
    peeked = _row()
    plan = _plan()
    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        _slot(None),
        patch.object(outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock()) as claim,
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock()) as post,
    ):
        assert await outbound_service.process_one("wx_a") is False
    plan.wait_to_dispatch.assert_not_awaited()
    claim.assert_not_called()
    post.assert_not_called()


async def test_peek_sleeps_while_row_still_pending(no_typing) -> None:
    peeked = _row(lease_gen=0)
    plan = _plan(delay=1.25)
    slept: list[float] = []

    async def capture_sleep(delay: float) -> None:
        slept.append(delay)

    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        patch.object(outbound_service.asyncio, "sleep", new=capture_sleep),
        _slot(),
        patch.object(
            outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock(return_value=None)
        ) as claim,
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock()) as post,
    ):
        assert await outbound_service.process_one("wx_a") is True
    assert slept == [1.25]
    claim.assert_awaited_once_with(peeked.id, peeked.lease_gen, app_id="slot_a")
    post.assert_not_called()


async def test_circuit_open_does_not_wait_or_send(no_typing) -> None:
    peeked = _row()
    plan = _plan(circuit=False)
    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        _slot(),
        patch.object(outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock()) as claim,
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock()) as post,
        patch.object(outbound_service.asyncio, "sleep", new=AsyncMock()) as slept,
    ):
        assert await outbound_service.process_one("wx_a") is False
    plan.wait_to_dispatch.assert_not_awaited()
    slept.assert_not_called()
    claim.assert_not_called()
    post.assert_not_called()


async def test_circuit_opens_during_wait_skips_claim(no_typing) -> None:
    peeked = _row()
    plan = _plan(delay=0.5)
    plan.circuit_ok = AsyncMock(side_effect=[True, False])
    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        patch.object(outbound_service.asyncio, "sleep", new=AsyncMock()),
        _slot(),
        patch.object(outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock()) as claim,
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock()) as post,
    ):
        assert await outbound_service.process_one("wx_a") is False
    claim.assert_not_called()
    post.assert_not_called()


async def test_claim_miss_does_not_send(no_typing) -> None:
    peeked = _row()
    plan = _plan()
    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        _slot(),
        patch.object(
            outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock(return_value=None)
        ),
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock()) as post,
    ):
        assert await outbound_service.process_one("wx_a") is True
    post.assert_not_called()


async def test_success_cas_mismatch_does_not_mark_delivered(no_typing) -> None:
    peeked = _row()
    claimed = _row(lease_gen=4)
    plan = _plan()
    ok = GeWeResult(ok=True, error_code=None, error_message="", new_msg_id="99")
    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        _slot(),
        patch.object(
            outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock(return_value=claimed)
        ),
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock(return_value=ok)),
        patch.object(
            outbound_service.outbox_crud, "mark_sent", new=AsyncMock(return_value=0)
        ) as upd,
        patch.object(outbound_service, "_safe_mark_delivered", new=AsyncMock()) as delivered,
    ):
        assert await outbound_service.process_one("wx_a") is True
    assert upd.await_args.args[0] == claimed.id
    assert upd.await_args.kwargs["lease_gen"] == 4
    delivered.assert_not_called()


async def test_success_cas_hit_marks_sent_and_delivered(no_typing) -> None:
    peeked = _row()
    claimed = _row(lease_gen=4)
    plan = _plan()
    ok = GeWeResult(ok=True, error_code=None, error_message="", new_msg_id="99")
    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        _slot(),
        patch.object(
            outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock(return_value=claimed)
        ),
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock(return_value=ok)),
        patch.object(outbound_service.outbox_crud, "mark_sent", new=AsyncMock(return_value=1)),
        patch.object(outbound_service, "_safe_mark_delivered", new=AsyncMock()) as delivered,
    ):
        assert await outbound_service.process_one("wx_a") is True
    delivered.assert_awaited_once_with("wx_a", "f1")


async def test_timeout_retries_once_when_attempts_remain(no_typing) -> None:
    peeked = _row()
    claimed = _row(attempt=1, lease_gen=2)
    plan = _plan()
    fail = GeWeResult(ok=False, error_code="gewe_timeout", error_message="timeout")
    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        _slot(),
        patch.object(
            outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock(return_value=claimed)
        ),
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock(return_value=fail)),
        patch.object(outbound_service.outbox_crud, "schedule_retry", new=AsyncMock()) as retry,
        patch.object(outbound_service.outbox_crud, "mark_failed", new=AsyncMock()) as failed,
    ):
        assert await outbound_service.process_one("wx_a") is True
    retry.assert_awaited_once()
    assert retry.await_args.kwargs["lease_gen"] == claimed.lease_gen
    failed.assert_not_called()


async def test_timeout_exhausted_marks_failed(no_typing) -> None:
    peeked = _row()
    claimed = _row(attempt=2, lease_gen=2)
    plan = _plan()
    fail = GeWeResult(ok=False, error_code="gewe_timeout", error_message="timeout")
    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        _slot(),
        patch.object(
            outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock(return_value=claimed)
        ),
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock(return_value=fail)),
        patch.object(outbound_service.outbox_crud, "schedule_retry", new=AsyncMock()) as retry,
        patch.object(outbound_service.outbox_crud, "mark_failed", new=AsyncMock()) as failed,
        patch.object(outbound_service.circuit_crud, "open_circuit", new=AsyncMock()) as circ,
    ):
        assert await outbound_service.process_one("wx_a") is True
    retry.assert_not_called()
    failed.assert_awaited_once()
    circ.assert_not_called()


async def test_offline_opens_node_circuit(no_typing) -> None:
    peeked = _row()
    claimed = _row(attempt=1, lease_gen=2)
    plan = _plan()
    fail = GeWeResult(ok=False, error_code="gewe_offline", error_message="offline")
    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        _slot(),
        patch.object(
            outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock(return_value=claimed)
        ),
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock(return_value=fail)),
        patch.object(outbound_service.outbox_crud, "mark_failed", new=AsyncMock()),
        patch.object(outbound_service.circuit_crud, "open_circuit", new=AsyncMock()) as circ,
    ):
        assert await outbound_service.process_one("wx_a") is True
    circ.assert_awaited_once()
    assert circ.await_args.args[0] == "wx_a"
    assert circ.await_args.kwargs["reason"] == "gewe_offline"


async def test_auth_does_not_open_node_circuit(no_typing) -> None:
    peeked = _row()
    claimed = _row(attempt=1, lease_gen=2)
    plan = _plan()
    fail = GeWeResult(ok=False, error_code="gewe_auth", error_message="token")
    with (
        patch.object(outbound_service.outbox_crud, "peek_next", new=AsyncMock(return_value=peeked)),
        patch.object(outbound_service, "for_account", return_value=plan),
        _slot(),
        patch.object(
            outbound_service.outbox_crud, "cas_claim_sending", new=AsyncMock(return_value=claimed)
        ),
        patch.object(outbound_service.gewe_client, "post_text", new=AsyncMock(return_value=fail)),
        patch.object(outbound_service.outbox_crud, "mark_failed", new=AsyncMock()),
        patch.object(outbound_service.circuit_crud, "open_circuit", new=AsyncMock()) as circ,
    ):
        assert await outbound_service.process_one("wx_a") is True
    circ.assert_not_called()


async def test_safe_mark_delivered_updates_latest_undelivered() -> None:
    with patch.object(
        outbound_service.chat_crud,
        "mark_last_assistant_delivered",
        new=AsyncMock(return_value=1),
    ) as mark:
        await outbound_service._safe_mark_delivered("wx_a", "f1")
    mark.assert_awaited_once_with(self_wxid="wx_a", friend_wxid="f1")


async def test_safe_mark_delivered_noop_when_none() -> None:
    with patch.object(
        outbound_service.chat_crud,
        "mark_last_assistant_delivered",
        new=AsyncMock(return_value=0),
    ) as mark:
        await outbound_service._safe_mark_delivered("wx_a", "f1")
    mark.assert_awaited_once_with(self_wxid="wx_a", friend_wxid="f1")


async def test_safe_mark_delivered_swallows_errors() -> None:
    with patch.object(
        outbound_service.chat_crud,
        "mark_last_assistant_delivered",
        new=AsyncMock(side_effect=RuntimeError("db")),
    ):
        await outbound_service._safe_mark_delivered("wx_a", "f1")
