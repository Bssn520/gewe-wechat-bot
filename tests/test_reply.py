from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import reply as reply_service
from app.utils.generation import LLMError
from app.utils.text_split import WECHAT_TEXT_MAX_BYTES

SESSION = ("wx_a", "f1")


def _row(id: int, content: str = "hi") -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        content=content,
        self_wxid="wx_a",
        friend_wxid="f1",
        claimed_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        lease_gen=1,
    )


@asynccontextmanager
async def _tx():
    yield None


def _async(value=None, *, error: Exception | None = None) -> AsyncMock:
    mock = AsyncMock()
    if error is not None:
        mock.side_effect = error
        return mock
    mock.return_value = value
    return mock


@contextmanager
def _pipeline(
    *,
    admit: bool | dict[str, bool],
    batch,
    extra=None,
    llm=None,
    llm_error=None,
    sessions: list[tuple[str, str]] | None = None,
    done_rows: int | None = None,
    remaining: float = 0.0,
):
    queue = list(sessions or [SESSION])

    async def peek(exclude_wxids=None, exclude_sessions=None):
        excluded = set(exclude_wxids or ())
        skipped = set(exclude_sessions or ())
        for session in queue:
            if session[0] not in excluded and session not in skipped:
                return session
        return None

    if isinstance(admit, dict):

        def _plan_for(self_wxid: str, domain: object) -> SimpleNamespace:
            return SimpleNamespace(admit=_async(admit[self_wxid]))

        for_account = patch.object(reply_service, "for_account", side_effect=_plan_for)
    else:
        for_account = patch.object(
            reply_service, "for_account", return_value=SimpleNamespace(admit=_async(admit))
        )
    claim = _async(batch)
    generate = _async("回" if llm is None else llm, error=llm_error)
    full = len(batch) + (0 if extra is None else len(extra))
    more = AsyncMock(side_effect=[extra or [], []])
    patches = [
        patch.object(reply_service.inbox_crud, "peek_next_session", new=peek),
        for_account,
        patch.object(reply_service.inbox_crud, "claim_session_batch", new=claim),
        patch.object(reply_service.chat_crud, "recent_delivered", new=_async([])),
        patch.object(reply_service.media_crud, "by_inbox_ids", new=_async([])),
        patch.object(reply_service, "generate_text", new=generate),
        patch.object(reply_service.inbox_crud, "mark_batch_failed", new=_async(0)),
        patch.object(reply_service.inbox_crud, "mark_batch_done", new=_async(done_rows or full)),
        patch.object(reply_service.outbox_crud, "create_pending", new=_async()),
        patch.object(reply_service.chat_crud, "append_turn", new=_async()),
        patch.object(reply_service, "in_transaction", _tx),
        patch.object(reply_service.asyncio, "sleep", new=_async()),
        patch.object(reply_service.inbox_crud, "claim_more_same_session", new=more),
        patch.object(reply_service.inbox_crud, "seconds_until", new=_async(remaining)),
    ]
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6] as failed,
        patches[7] as done,
        patches[8] as outbox,
        patches[9] as append,
        patches[10],
        patches[11] as slept,
        patches[12],
        patches[13],
    ):
        yield SimpleNamespace(
            claim=claim,
            generate=generate,
            failed=failed,
            done=done,
            outbox=outbox,
            append=append,
            slept=slept,
            more=more,
        )


@pytest.fixture(autouse=True)
def _no_debounce(monkeypatch) -> None:
    monkeypatch.setattr(reply_service.settings, "DEBOUNCE_MS", 0)


async def test_process_one_batch_empty_peek_returns_false() -> None:
    with patch.object(reply_service.inbox_crud, "peek_next_session", new=_async(None)):
        assert await reply_service.process_one_batch("w1") is False


async def test_admit_denied_does_not_claim() -> None:
    with _pipeline(admit=False, batch=[]) as p:
        assert await reply_service.process_one_batch("w1") is False
    p.claim.assert_not_called()


async def test_admit_denied_skips_to_next_account() -> None:
    batch = [_row(9, "hi")]
    err = LLMError("llm_empty")
    with _pipeline(
        admit={"wx_a": False, "wx_b": True},
        batch=batch,
        sessions=[("wx_a", "f1"), ("wx_b", "f2")],
        llm_error=err,
    ) as p:
        assert await reply_service.process_one_batch("w1") is True
    p.claim.assert_awaited_once()
    assert p.claim.await_args.kwargs["self_wxid"] == "wx_b"
    assert p.claim.await_args.kwargs["friend_wxid"] == "f2"


async def test_claim_empty_returns_true_without_llm() -> None:
    with _pipeline(admit=True, batch=[]) as p:
        assert await reply_service.process_one_batch("w1") is True
    p.generate.assert_not_called()


async def test_llm_error_marks_inbox_failed_terminal() -> None:
    batch = [_row(11, "问1"), _row(12, "问2")]
    err = LLMError("llm_timeout", "slow")
    with _pipeline(admit=True, batch=batch, llm_error=err) as p:
        assert await reply_service.process_one_batch("w1") is True
    p.failed.assert_awaited_once()
    assert p.failed.await_args.args[0] == [11, 12]
    assert p.failed.await_args.kwargs["error_code"] == "llm_timeout"
    p.done.assert_not_called()
    p.outbox.assert_not_called()
    p.append.assert_not_called()


async def test_unknown_exception_maps_to_llm_unknown() -> None:
    with _pipeline(admit=True, batch=[_row(3)], llm_error=RuntimeError("boom")) as p:
        assert await reply_service.process_one_batch("w1") is True
    assert p.failed.await_args.kwargs["error_code"] == "llm_unknown"


async def test_success_writes_history_outbox_and_marks_done() -> None:
    batch = [_row(1, "一"), _row(2, "二")]
    with _pipeline(admit=True, batch=batch, llm="回") as p:
        assert await reply_service.process_one_batch("w1") is True
    p.append.assert_awaited_once()
    assert p.append.await_args.kwargs["user_content"] == "一\n二"
    assert p.append.await_args.kwargs["assistant_content"] == "回"
    p.outbox.assert_awaited_once()
    assert p.outbox.await_args.kwargs["source_inbox_ids"] == [1, 2]
    p.done.assert_awaited_once_with([1, 2], [1, 1])


async def test_debounce_claims_more_same_session(monkeypatch) -> None:
    monkeypatch.setattr(reply_service.settings, "DEBOUNCE_MS", 10)
    first = [_row(1, "a")]
    extra = [_row(2, "b")]
    err = LLMError("llm_empty")
    with _pipeline(admit=True, batch=first, extra=extra, llm_error=err, remaining=0.01) as p:
        await reply_service.process_one_batch("w1")
    p.slept.assert_awaited()
    assert p.failed.await_args.args[0] == [1, 2]


async def test_long_reply_splits_into_multiple_outbox_rows() -> None:
    """长报告拆成 N 条 outbox 行，但**历史里存完整文本**。"""
    long_report = "\n\n".join(
        [f"## 第{i}节\n" + "脾虚痰湿，水湿内停，阳气不足。" * 40 for i in range(1, 5)]
    )
    batch = [_row(1, "看下舌苔")]
    with _pipeline(admit=True, batch=batch, llm=long_report) as p:
        assert await reply_service.process_one_batch("w1") is True

    # 拆成多条待发，每条都不超微信单条字节上限
    parts = [c.kwargs["content"] for c in p.outbox.await_args_list]
    assert len(parts) > 1
    assert all(len(x.encode("utf-8")) <= WECHAT_TEXT_MAX_BYTES for x in parts)

    # 历史仍是完整文本（不做拆条）——这是不做图片回挂的前提
    assert p.append.await_args.kwargs["assistant_content"] == long_report

    # 每条都带同一批 source_inbox_ids，便于溯源
    for call in p.outbox.await_args_list:
        assert call.kwargs["source_inbox_ids"] == [1]


async def test_short_reply_stays_single_outbox_row() -> None:
    """日常短回复不受拆条影响：只有 1 条 outbox 行（回归保护）。"""
    with _pipeline(admit=True, batch=[_row(1, "在吗")], llm="在的，有什么事？") as p:
        await reply_service.process_one_batch("w1")
    p.outbox.assert_awaited_once()
    assert p.outbox.await_args.kwargs["content"] == "在的，有什么事？"
