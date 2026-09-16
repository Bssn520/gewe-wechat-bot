from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models.media import (
    MEDIA_FAILED,
    MEDIA_IMAGE,
    MEDIA_READY,
    MEDIA_SKIPPED,
)
from app.services import media as media_service
from app.utils.generation import _to_messages


def _m(**kw) -> SimpleNamespace:
    base: dict = {
        "id": 1,
        "inbox_id": 1,
        "media_type": MEDIA_IMAGE,
        "status": MEDIA_READY,
        "asset_ref": "http://cdn/x.png",
        "text_repr": None,
        "source": {"xml": "<msg/>"},
    }
    return SimpleNamespace(**{**base, **kw})


# --- render：占位与失败文案 ---


def test_render_ready_image_is_placeholder() -> None:
    assert media_service.render(_m(status=MEDIA_READY)) == "[图片]"


def test_render_ready_with_text_repr_appends_transcript() -> None:
    out = media_service.render(_m(status=MEDIA_READY, text_repr="你好"))
    assert out == "[图片]（你好）"


def test_render_failed_says_failed_not_plain_placeholder() -> None:
    """失败必须写明「加载失败」：只给 [图片] 会诱导模型编造图片内容。"""
    for status in (MEDIA_FAILED, MEDIA_SKIPPED):
        out = media_service.render(_m(status=status))
        assert "加载失败" in out
        assert out != "[图片]"


def test_render_pending_is_bare_placeholder() -> None:
    assert media_service.render(_m(status="pending")) == "[图片]"


# --- 并发/限速常量 ---


def test_download_interval_is_within_official_bounds() -> None:
    """官方要求账号内每条下载间隔 3~10 秒（越界有掉线风险）。"""
    low, high = media_service.DOWNLOAD_INTERVAL_SECONDS
    assert low >= 3.0
    assert high <= 10.0
    for _ in range(20):
        assert low <= media_service.interval_seconds() <= high


def test_image_quality_fallback_starts_with_normal() -> None:
    """从常规图起步：高清更贵更慢，不用作首选。"""
    assert media_service.IMAGE_QUALITY_FALLBACK[0] == 2


# --- process_one 状态机 ---


async def test_process_one_skips_when_backlog_exceeded(monkeypatch) -> None:
    """积压超限直接标 skipped（终态），不再下载。"""
    monkeypatch.setattr(media_service, "DOWNLOAD_BACKLOG_PER_ACCOUNT", 0)
    media = _m()
    with (
        patch.object(media_service.media_crud, "pending_count", new=AsyncMock(return_value=5)),
        patch.object(media_service.media_crud, "next_pending", new=AsyncMock(return_value=media)),
        patch.object(media_service.media_crud, "mark_skipped", new=AsyncMock()) as skipped,
        patch.object(media_service, "_fetch", new=AsyncMock()) as fetch,
    ):
        assert await media_service.process_one("wx_a") is True
    skipped.assert_awaited_once()
    fetch.assert_not_awaited()


async def test_process_one_skips_when_daily_cap_reached(monkeypatch) -> None:
    monkeypatch.setattr(media_service, "DOWNLOAD_DAILY_CAP", 1)
    media = _m()
    with (
        patch.object(media_service.media_crud, "pending_count", new=AsyncMock(return_value=1)),
        patch.object(media_service.media_crud, "next_pending", new=AsyncMock(return_value=media)),
        patch.object(media_service.media_crud, "attempted_today", new=AsyncMock(return_value=1)),
        patch.object(media_service.media_crud, "mark_skipped", new=AsyncMock()) as skipped,
    ):
        await media_service.process_one("wx_a")
    assert skipped.await_args.kwargs["error_code"] == "media_daily_cap"


async def test_process_one_marks_ready_on_success() -> None:
    media = _m()
    with (
        patch.object(media_service.media_crud, "pending_count", new=AsyncMock(return_value=1)),
        patch.object(media_service.media_crud, "next_pending", new=AsyncMock(return_value=media)),
        patch.object(media_service.media_crud, "attempted_today", new=AsyncMock(return_value=0)),
        patch.object(media_service.media_crud, "bump_attempt", new=AsyncMock()),
        patch.object(media_service.node_crud, "get_app_id", new=AsyncMock(return_value="app1")),
        patch.object(
            media_service,
            "_fetch",
            new=AsyncMock(return_value=(True, "http://cdn/x.png", "", None)),
        ),
        patch.object(media_service.media_crud, "mark_ready", new=AsyncMock()) as ready,
    ):
        assert await media_service.process_one("wx_a") is True
    assert ready.await_args.kwargs["asset_ref"] == "http://cdn/x.png"


async def test_process_one_marks_failed_when_no_slot() -> None:
    """没有槽位也要标 failed，否则该会话会被生成侧一直 defer。"""
    media = _m()
    with (
        patch.object(media_service.media_crud, "pending_count", new=AsyncMock(return_value=1)),
        patch.object(media_service.media_crud, "next_pending", new=AsyncMock(return_value=media)),
        patch.object(media_service.media_crud, "attempted_today", new=AsyncMock(return_value=0)),
        patch.object(media_service.node_crud, "get_app_id", new=AsyncMock(return_value=None)),
        patch.object(media_service.media_crud, "mark_failed", new=AsyncMock()) as failed,
    ):
        await media_service.process_one("wx_a")
    assert failed.await_args.kwargs["error_code"] == "media_no_slot"


# --- 图片注入：必须挂在 user Message 上（否则被 agno 静默丢弃）---


def test_images_attach_to_last_user_message() -> None:
    msgs = _to_messages(
        [{"role": "user", "content": "看看这个 [图片]"}],
        ["http://cdn/a.png"],
    )
    assert len(msgs) == 1
    images = msgs[-1].images
    assert images is not None and len(images) == 1
    assert images[0].url == "http://cdn/a.png"


def test_no_images_when_none_passed() -> None:
    msgs = _to_messages([{"role": "user", "content": "hi"}], None)
    assert msgs[-1].images is None


def test_system_is_dropped_and_images_go_to_last_user() -> None:
    msgs = _to_messages(
        [
            {"role": "system", "content": "人设"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "在"},
            {"role": "user", "content": "[图片]"},
        ],
        ["http://cdn/a.png"],
    )
    assert all(m.role != "system" for m in msgs)
    assert msgs[-1].role == "user"
    assert msgs[-1].images is not None
    # 前面的消息不带图
    assert all(m.images is None for m in msgs[:-1])
