"""媒体取用与渲染。

平铺实现，不做 handler/注册表抽象：媒体类型只有图片/语音/视频三种，语义一致
（取下来 → 存储引用 → 渲染成占位）。新增语音时写一个 `_download_voice()` 并在
`_fetch` 里加一个分支即可。

限速：下载消耗的是**账号会话**（GeWe 用你的账号去微信 CDN 取文件），官方要求
串行 + 每条 3~10 秒，频率高会掉线。所以限速按 self_wxid、且独立于发送域。
"""

from __future__ import annotations

import random

import structlog

from app.crud import media as media_crud
from app.crud import node as node_crud
from app.models.media import (
    MEDIA_FAILED,
    MEDIA_IMAGE,
    MEDIA_READY,
    MEDIA_SKIPPED,
    Media,
)
from app.utils.gewe_client import gewe_client

logger = structlog.get_logger(__name__)

# 占位符：进模型的文本表示
PLACEHOLDERS: dict[str, str] = {
    MEDIA_IMAGE: "[图片]",
}
# 下载档位回退链：常规 → 高清 → 缩略图（官方：不是所有图都有高清/常规版本）
IMAGE_QUALITY_FALLBACK: tuple[int, ...] = (2, 1, 3)
# 账号内两条下载的最小/最大间隔（秒）。这是安全底线，不是可随意调的偏好。
DOWNLOAD_INTERVAL_SECONDS: tuple[float, float] = (3.0, 10.0)
# 该账号 pending 积压超过此数则跳过（走失败文案），避免队列无界增长
DOWNLOAD_BACKLOG_PER_ACCOUNT: int = 30
# 该账号每日下载上限（业务日按 QUOTA_TZ）：间隔只限速率，这层限总量
DOWNLOAD_DAILY_CAP: int = 200
# 单次生成最多带几张图（多模态 token 与延迟考虑）
MEDIA_MAX_PER_TURN: int = 2
# pending 超时：必须区分「排队中」与「真卡死」，否则串行下载下排队中的图会被
# 误判死亡——有效容量被压到约 5 张（30s / 平均 6.5s），连发多图的「分析这几张」
# 会大面积失败，且 pending 提前清零会让会话在图没取全时就回复。
#
# 判据是 attempt：`bump_attempt` 在真正发起下载**之前**执行，所以
#   attempt > 0 → 我们动过它但没收到终态 → 按「卡死」判（本常量）
#   attempt = 0 → 还在排队 → 由下面的排队上限判（容量 × 最坏间隔 + 余量）
MEDIA_ATTEMPT_TIMEOUT_SECONDS: float = 30.0
# 从未被尝试过的排队上限：约 30 张 × 10s 最坏间隔 + 余量。
# 兜底防的是「下载循环挂掉导致该行永远拿不到 attempt」，那会永久 defer 该会话。
MEDIA_QUEUE_TIMEOUT_SECONDS: float = 420.0


def render(media: Media) -> str:
    """媒体的文本表示，用于组装进模型上下文。"""
    placeholder = PLACEHOLDERS.get(media.media_type, "[媒体]")
    if media.status == MEDIA_READY:
        if media.text_repr:
            return f"{placeholder}（{media.text_repr}）"
        return placeholder
    if media.status in (MEDIA_FAILED, MEDIA_SKIPPED):
        # 明确「加载失败」：绝不能只给占位符，否则模型会凭空编造内容
        return f"{placeholder}加载失败，无法查看"
    # pending：正常路径已被 peek 的 defer 挡住，这里只是兜底
    return placeholder


def interval_seconds() -> float:
    return random.uniform(*DOWNLOAD_INTERVAL_SECONDS)


async def _download_image(media: Media, app_id: str) -> tuple[bool, str | None, str]:
    """按档位回退链取图。返回 (ok, asset_ref, error_message)。"""
    xml = str(media.source.get("xml") or "")
    last_err = ""
    for quality in IMAGE_QUALITY_FALLBACK:
        result = await gewe_client.download_image(app_id=app_id, xml=xml, type=quality)
        if result.ok and result.file_url:
            return True, result.file_url, ""
        last_err = result.error_code or ""
        logger.info(
            "media.image_quality_failed",
            media_id=media.id,
            quality=quality,
            error_code=result.error_code,
        )
    return False, None, last_err or "media_download_failed"


async def _fetch(media: Media, app_id: str) -> tuple[bool, str | None, str, str | None]:
    """按类型取媒体。返回 (ok, asset_ref, error_message, text_repr)。

    新增语音/视频：在这里加一个 elif 分支 + 写一个 `_download_voice()`。
    """
    if media.media_type == MEDIA_IMAGE:
        ok, ref, err = await _download_image(media, app_id)
        return ok, ref, err, None
    return False, None, f"unsupported_media_type:{media.media_type}", None


async def process_one(self_wxid: str) -> bool:
    """取该账号一条待处理媒体。True 表示本轮有活干。"""
    pending = await media_crud.pending_count(self_wxid)
    if pending == 0:
        return False

    media = await media_crud.next_pending(self_wxid)
    if media is None:
        return False

    if pending > DOWNLOAD_BACKLOG_PER_ACCOUNT:
        await media_crud.mark_skipped(media.id)
        logger.warning(
            "media.backlog_skipped", self_wxid=self_wxid, media_id=media.id, pending=pending
        )
        return True

    # 日配额：间隔只限速率（3~10s 仍可日调用数千次），这层限总量
    if await media_crud.attempted_today(self_wxid) >= DOWNLOAD_DAILY_CAP:
        await media_crud.mark_skipped(media.id, error_code="media_daily_cap")
        logger.warning("media.daily_cap_skipped", self_wxid=self_wxid, media_id=media.id)
        return True

    app_id = await node_crud.get_app_id(self_wxid)
    if not app_id:
        # 没有槽位（掉线/回收中）：标失败，避免该会话被永久 defer
        await media_crud.mark_failed(media.id, error_code="media_no_slot")
        logger.error("media.no_slot", self_wxid=self_wxid, media_id=media.id)
        return True

    await media_crud.bump_attempt(media.id)
    ok, asset_ref, err, text_repr = await _fetch(media, app_id)
    if ok and asset_ref:
        await media_crud.mark_ready(media.id, asset_ref=asset_ref, text_repr=text_repr)
        logger.info(
            "media.ready", self_wxid=self_wxid, media_id=media.id, media_type=media.media_type
        )
        return True

    await media_crud.mark_failed(media.id, error_code=err or "media_unknown")
    logger.warning("media.failed", self_wxid=self_wxid, media_id=media.id, error_code=err)
    return True


__all__ = [
    "MEDIA_ATTEMPT_TIMEOUT_SECONDS",
    "MEDIA_MAX_PER_TURN",
    "MEDIA_QUEUE_TIMEOUT_SECONDS",
    "PLACEHOLDERS",
    "interval_seconds",
    "process_one",
    "render",
]
