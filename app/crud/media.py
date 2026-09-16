"""媒体行 CRUD。只做 filter/create/update，不含业务判断。"""

from __future__ import annotations

from tortoise.transactions import in_transaction

from app.core.config import settings
from app.models.media import (
    MEDIA_FAILED,
    MEDIA_PENDING,
    MEDIA_READY,
    MEDIA_SKIPPED,
    Media,
)


async def by_inbox_ids(ids: list[int]) -> list[Media]:
    """按入站 id 批量取媒体，供组装时渲染占位。"""
    if not ids:
        return []
    return await Media.filter(inbox_id__in=ids)


async def next_pending(self_wxid: str) -> Media | None:
    """该账号最老的一条待取媒体（id 升序，让最老会话先就绪）。"""
    return await Media.filter(self_wxid=self_wxid, status=MEDIA_PENDING).order_by("id").first()


async def pending_count(self_wxid: str) -> int:
    return await Media.filter(self_wxid=self_wxid, status=MEDIA_PENDING).count()


async def bump_attempt(media_id: int) -> int:
    """记一次取用。返回受影响行数。"""
    async with in_transaction() as conn:
        result = await conn.execute_query(
            "UPDATE media SET attempt = attempt + 1, updated_at = NOW() WHERE id = $1",
            [media_id],
        )
    return int(result[0] or 0)


async def mark_ready(media_id: int, *, asset_ref: str, text_repr: str | None = None) -> int:
    """pending → ready。时间戳走 DB NOW()，与静默窗/超时判定同源（避免钟差）。"""
    async with in_transaction() as conn:
        result = await conn.execute_query(
            """
            UPDATE media
            SET status = $1, asset_ref = $2, text_repr = $3,
                error_code = NULL, error_message = NULL, updated_at = NOW()
            WHERE id = $4 AND status = $5
            """,
            [MEDIA_READY, asset_ref, text_repr, media_id, MEDIA_PENDING],
        )
    return int(result[0] or 0)


async def mark_failed(media_id: int, *, error_code: str, error_message: str = "") -> int:
    async with in_transaction() as conn:
        result = await conn.execute_query(
            """
            UPDATE media
            SET status = $1, error_code = $2, error_message = $3, updated_at = NOW()
            WHERE id = $4 AND status = $5
            """,
            [MEDIA_FAILED, error_code, (error_message or "")[:500], media_id, MEDIA_PENDING],
        )
    return int(result[0] or 0)


async def mark_skipped(media_id: int, *, error_code: str = "media_backlog") -> int:
    """积压超限时的终态：走失败文案，不再尝试。"""
    async with in_transaction() as conn:
        result = await conn.execute_query(
            """
            UPDATE media
            SET status = $1, error_code = $2, updated_at = NOW()
            WHERE id = $3 AND status = $4
            """,
            [MEDIA_SKIPPED, error_code, media_id, MEDIA_PENDING],
        )
    return int(result[0] or 0)


async def attempted_today(self_wxid: str) -> int:
    """该账号今天（业务日，QUOTA_TZ）已尝试下载的条数。"""
    async with in_transaction() as conn:
        rows = await conn.execute_query_dict(
            """
            SELECT COUNT(*) AS n
            FROM media
            WHERE self_wxid = $1
              AND attempt > 0
              AND (updated_at AT TIME ZONE $2)::date = (NOW() AT TIME ZONE $2)::date
            """,
            [self_wxid, settings.QUOTA_TZ],
        )
    return int(rows[0]["n"]) if rows else 0


async def fail_stale(attempt_timeout_s: float, queue_timeout_s: float) -> int:
    """把超时未就绪的 pending 标 failed，解除生成侧 defer。

    必须区分两种「超时」，否则串行下载下排队中的图会被误判死亡：
    - `attempt > 0`：已发起过下载但没收到终态 → 按**卡死**判（attempt_timeout_s）
    - `attempt = 0`：还在排队 → 按**排队上限**判（queue_timeout_s，约等于积压容量
      × 最坏下载间隔）

    后者兜底的场景是「下载循环挂掉导致该行永远拿不到 attempt」——那会永久 defer
    该会话，比误判更糟。
    """
    async with in_transaction() as conn:
        result = await conn.execute_query(
            """
            UPDATE media
            SET status = $1,
                error_code = $2,
                error_message = $3,
                updated_at = NOW()
            WHERE status = $4
              AND (
                (attempt > 0 AND updated_at + ($5 * INTERVAL '1 second') < NOW())
                OR (attempt = 0 AND created_at + ($6 * INTERVAL '1 second') < NOW())
              )
            """,
            [
                MEDIA_FAILED,
                "media_timeout",
                "pending 超时未取到",
                MEDIA_PENDING,
                attempt_timeout_s,
                queue_timeout_s,
            ],
        )
    return int(result[0] or 0)
