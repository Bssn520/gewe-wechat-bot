from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import structlog
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from app.core.config import settings
from app.core.queue_constants import (
    INBOX_DONE,
    INBOX_FAILED,
    INBOX_PENDING,
    INBOX_PROCESSING,
    MEDIA_QUIET_MS,
)
from app.models.inbox import Inbox
from app.models.media import MEDIA_PENDING

logger = structlog.get_logger(__name__)


async def insert_pending(
    *,
    self_wxid: str,
    app_id: str,
    friend_wxid: str,
    new_msg_id: str,
    content: str | None,
    media_type: str | None = None,
    media_source: dict[str, Any] | None = None,
) -> Inbox | None:
    """唯一键冲突返回 None（重复回调）。created_at 走 DB NOW()，与 claimed_at 同源。

    `media_type` 非空时，**同一事务**内再建一条 media 行（1:1），保证「有消息
    必有媒体行」；冲突时不建，靠 inbox 唯一键做幂等。媒体消息的 `content` 传 None。
    """
    async with in_transaction() as conn:
        try:
            rows = await conn.execute_query_dict(
                """
                INSERT INTO inbox (
                    self_wxid, app_id, friend_wxid, new_msg_id, content,
                    status, attempt, lease_gen, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, 0, 0, NOW(), NOW())
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                [self_wxid, app_id, friend_wxid, new_msg_id, content, INBOX_PENDING],
            )
        except IntegrityError as exc:
            logger.info(
                "inbox.duplicate",
                self_wxid=self_wxid,
                app_id=app_id,
                new_msg_id=new_msg_id,
                error=str(exc),
            )
            return None
        if rows and media_type:
            await conn.execute_query(
                """
                INSERT INTO media (
                    inbox_id, self_wxid, friend_wxid, media_type, status,
                    source, attempt, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, 0, NOW(), NOW())
                """,
                [
                    rows[0]["id"],
                    self_wxid,
                    friend_wxid,
                    media_type,
                    MEDIA_PENDING,
                    json.dumps(media_source or {}),
                ],
            )
    if not rows:
        logger.info(
            "inbox.duplicate",
            self_wxid=self_wxid,
            app_id=app_id,
            new_msg_id=new_msg_id,
        )
        return None
    return await Inbox.get(id=rows[0]["id"])


async def mark_batch_failed(
    ids: list[int], gens: list[int], *, error_code: str, error_message: str
) -> int:
    return await _mark_batch_terminal(
        ids,
        gens,
        status=INBOX_FAILED,
        error_code=error_code,
        error_message=error_message[:500],
    )


async def mark_batch_done(ids: list[int], gens: list[int]) -> int:
    return await _mark_batch_terminal(ids, gens, status=INBOX_DONE)


async def _mark_batch_terminal(
    ids: list[int],
    gens: list[int],
    *,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    """按 (id, lease_gen) 成对匹配回写终态，返回实际影响行数。

    同批内每行代次可能不同（首次认领与 debounce 补领各自 +1），故必须成对匹配；
    代次对不上说明租约已被回收/接管，调用方据此回滚，不得覆盖新持有者。
    """
    if not ids:
        return 0
    if len(ids) != len(gens):
        raise ValueError("ids 与 gens 必须等长")
    async with in_transaction() as conn:
        result = await conn.execute_query(
            """
            UPDATE inbox AS i
            SET status = $1,
                error_code = $2,
                error_message = $3,
                leased_until = NULL,
                updated_at = NOW()
            FROM unnest($4::bigint[], $5::int[]) AS v(id, gen)
            WHERE i.id = v.id AND i.lease_gen = v.gen AND i.status = $6
            """,
            [status, error_code, error_message, ids, gens, INBOX_PROCESSING],
        )
    return int(result[0] or 0)


async def peek_next_session(
    exclude_wxids: set[str] | frozenset[str] | None = None,
    exclude_sessions: set[tuple[str, str]] | frozenset[tuple[str, str]] | None = None,
) -> tuple[str, str] | None:
    """找最老的、所在账号尚未满员、且没有 processing 行的 pending 会话。不改状态。"""
    per = settings.INBOX_CONCURRENCY_PER_ACCOUNT
    global_cap = settings.INBOX_CONCURRENCY_GLOBAL
    excluded_wxids = list(exclude_wxids or ())
    skipped = list(exclude_sessions or ())
    skip_selfs = [pair[0] for pair in skipped]
    skip_friends = [pair[1] for pair in skipped]
    async with in_transaction() as conn:
        global_busy = await conn.execute_query_dict(
            "SELECT COUNT(DISTINCT (self_wxid, friend_wxid)) AS n FROM inbox WHERE status = $1",
            [INBOX_PROCESSING],
        )
        if int(global_busy[0]["n"] if global_busy else 0) >= global_cap:
            return None
        rows = await conn.execute_query_dict(
            """
            SELECT i.self_wxid, i.friend_wxid, MIN(i.id) AS first_id
            FROM inbox i
            WHERE i.status = $1
              AND NOT EXISTS (
                SELECT 1 FROM inbox p
                WHERE p.self_wxid = i.self_wxid
                  AND p.friend_wxid = i.friend_wxid
                  AND p.status = $2
              )
              -- 本会话还有媒体没取到就先不认领：让「发图求分析」一定带图。
              -- 用 SQL 而非内存记账：生成循环会重启，内存集合一重启就丢。
              -- 媒体**全部解析完之后再按住一个静默窗**，把「解析期间用户补发的
              -- 问题」收进同一批，避免图文被拆成两轮各自回复。解除靠 $7 的静默窗
              -- 走完，或 media 侧超时翻 failed（见 crud/media.fail_stale）。
              AND NOT EXISTS (
                SELECT 1 FROM media m
                WHERE m.self_wxid = i.self_wxid
                  AND m.friend_wxid = i.friend_wxid
                  AND (
                    m.status = $7
                    OR m.updated_at > NOW() - ($8 * INTERVAL '1 millisecond')
                  )
              )
              AND (
                SELECT COUNT(DISTINCT p.friend_wxid)
                FROM inbox p
                WHERE p.self_wxid = i.self_wxid AND p.status = $2
              ) < $3
              AND (CARDINALITY($4::text[]) = 0 OR i.self_wxid <> ALL($4::text[]))
              AND (
                CARDINALITY($5::text[]) = 0
                OR (i.self_wxid, i.friend_wxid) NOT IN (
                  SELECT t.s, t.f FROM unnest($5::text[], $6::text[]) AS t(s, f)
                )
              )
            GROUP BY i.self_wxid, i.friend_wxid
            ORDER BY first_id
            LIMIT 1
            """,
            [
                INBOX_PENDING,
                INBOX_PROCESSING,
                per,
                excluded_wxids,
                skip_selfs,
                skip_friends,
                MEDIA_PENDING,
                MEDIA_QUIET_MS,
            ],
        )
    if not rows:
        return None
    return rows[0]["self_wxid"], rows[0]["friend_wxid"]


def _sliding_chain_sql(*, debounce_idx: int, after_idx: int | None = None) -> str:
    """相邻间隔 ≤ DEBOUNCE_MS 的第一段连续链。

    从待认领里按到达顺序走：第一条起算，下一条与上一条时差超过窗宽就切断。
    `after_idx` 有值时，链从该时刻之后接着走（补领：上一条已认领的到达时刻）。
    """
    prev = (
        f"LAG(created_at, 1, ${after_idx}::timestamptz) OVER (ORDER BY created_at, id)"
        if after_idx is not None
        else "LAG(created_at) OVER (ORDER BY created_at, id)"
    )
    newer = f"AND created_at > ${after_idx}" if after_idx is not None else ""
    gap = f"o.created_at <= o.prev_at + (${debounce_idx} * INTERVAL '1 millisecond')"
    first_gap = gap if after_idx is not None else f"o.prev_at IS NULL OR {gap}"
    return f"""
              AND id IN (
                SELECT g.id FROM (
                  SELECT
                    o.id,
                    SUM(
                      CASE WHEN {first_gap} THEN 0 ELSE 1 END
                    ) OVER (ORDER BY o.created_at, o.id) AS grp
                  FROM (
                    SELECT id, created_at, {prev} AS prev_at
                    FROM inbox
                    WHERE status = $4 AND self_wxid = $5 AND friend_wxid = $6
                      {newer}
                  ) o
                ) g
                WHERE g.grp = 0
              )
    """


async def claim_session_batch(*, worker_id: str, self_wxid: str, friend_wxid: str) -> list[Inbox]:
    """首次认领：该会话已有 processing 则空手。

    `DEBOUNCE_MS > 0` 时只收**相邻间隔不超过窗宽**的第一段连续链：从最早待认领
    起走，下一条与上一条时差超过 `DEBOUNCE_MS` 就切断。整段可以跨过远长于窗宽
    的墙钟（每条都隔 8 秒、窗 10 秒 → 仍并成一批），但中间裂开超过窗宽的两条
    不会合成一句。

    `DEBOUNCE_MS=0`：不设窗口，待认领全收。媒体静默仍只在 peek 侧按住会话，
    不参与本层切断——图文是否并批，看它们的到达间隔。
    """
    debounce_ms = settings.DEBOUNCE_MS
    window_sql = ""
    extra_params: list[object] = []
    if debounce_ms > 0:
        # $8 = 相邻间隔上限（额外参数从 $8 起，见 _claim_pending）
        window_sql = _sliding_chain_sql(debounce_idx=8)
        extra_params = [debounce_ms]
    return await _claim_pending(
        self_wxid=self_wxid,
        friend_wxid=friend_wxid,
        worker_id=worker_id,
        extra_sql="""
              AND NOT EXISTS (
                SELECT 1 FROM inbox p
                WHERE p.self_wxid = $5 AND p.friend_wxid = $6 AND p.status = $1
              )
        """
        + window_sql,
        extra_params=extra_params,
    )


async def claim_more_same_session(
    *,
    self_wxid: str,
    friend_wxid: str,
    worker_id: str,
    after: datetime | None = None,
    limit: int | None = None,
) -> list[Inbox]:
    """debounce 补领：允许本会话已有 processing。

    `after` 有值时只收「接在这条到达时刻之后、相邻间隔仍 ≤ DEBOUNCE_MS」的链。
    右界随最后一条往后滑，不钉在首条到达时刻。
    """
    extra_sql = ""
    extra_params: list[object] = []
    if after is not None and settings.DEBOUNCE_MS > 0:
        extra_sql = _sliding_chain_sql(debounce_idx=9, after_idx=8)
        extra_params = [after, settings.DEBOUNCE_MS]
    return await _claim_pending(
        self_wxid=self_wxid,
        friend_wxid=friend_wxid,
        worker_id=worker_id,
        extra_sql=extra_sql,
        extra_params=extra_params,
        limit=limit,
    )


async def _claim_pending(
    *,
    self_wxid: str,
    friend_wxid: str,
    worker_id: str,
    extra_sql: str = "",
    extra_params: list[object] | None = None,
    limit: int | None = None,
) -> list[Inbox]:
    batch = settings.INBOX_BATCH_MAX if limit is None else limit
    if batch <= 0:
        return []
    lease = settings.INBOX_LEASE_SECONDS
    params: list[object] = [
        INBOX_PROCESSING,
        lease,
        worker_id,
        INBOX_PENDING,
        self_wxid,
        friend_wxid,
        batch,
        *(extra_params or ()),
    ]
    # 账号级并发上限在认领这一层再判一次（peek 的判定到认领之间有一次 DB 往返，
    # 仅靠 peek 会放过第 N+1 个会话）。占位符按下标拼，避免与 extra_params 抢号。
    cap_idx = len(params) + 1
    params.append(settings.INBOX_CONCURRENCY_PER_ACCOUNT)
    async with in_transaction() as conn:
        # 事务级 advisory 锁按账号串行化认领：并发认领读不到对方未提交的 processing 行，
        # 锁住后第二个事务才能看到第一个已提交的结果，上限判定才准确。提交即自动释放。
        await conn.execute_query("SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", [self_wxid])
        claimed = await conn.execute_query_dict(
            f"""
            UPDATE inbox
            SET status = $1,
                leased_until = NOW() + ($2 * INTERVAL '1 second'),
                lease_gen = lease_gen + 1,
                attempt = attempt + 1,
                worker_id = $3,
                claimed_at = COALESCE(claimed_at, NOW()),
                updated_at = NOW()
            WHERE id IN (
              SELECT id FROM inbox
              WHERE status = $4 AND self_wxid = $5 AND friend_wxid = $6
              {extra_sql}
              AND (
                SELECT COUNT(DISTINCT o.friend_wxid) FROM inbox o
                WHERE o.self_wxid = $5 AND o.status = $1 AND o.friend_wxid <> $6
              ) < ${cap_idx}
              ORDER BY id
              LIMIT $7
              FOR UPDATE SKIP LOCKED
            )
            RETURNING id, claimed_at
            """,
            params,
        )
    ids = [row["id"] for row in claimed]
    if not ids:
        return []
    return await Inbox.filter(id__in=ids).order_by("id")


def debounce_window_end(anchor_created: datetime, debounce_ms: int) -> datetime:
    """滑窗静默右界：锚在这条到达时刻 + DEBOUNCE_MS。补领时锚是当前批最后一条。"""
    return anchor_created + timedelta(milliseconds=debounce_ms)


async def seconds_until(deadline: datetime) -> float:
    """距离 deadline 还剩几秒（负数表示已过期）。两端都走 DB NOW()，避免混钟。"""
    async with in_transaction() as conn:
        rows = await conn.execute_query_dict(
            "SELECT EXTRACT(EPOCH FROM ($1 - NOW())) AS s",
            [deadline],
        )
    return float(rows[0]["s"] if rows else 0.0)


async def reclaim_stale() -> int:
    grace = settings.INBOX_LEASE_GRACE_SECONDS
    async with in_transaction() as conn:
        result = await conn.execute_query(
            """
            UPDATE inbox
            SET status = $1,
                leased_until = NULL,
                lease_gen = lease_gen + 1,
                updated_at = NOW()
            WHERE status = $2
              AND leased_until IS NOT NULL
              AND leased_until + ($3 * INTERVAL '1 second') < NOW()
            """,
            [INBOX_PENDING, INBOX_PROCESSING, grace],
        )
    return int(result[0] or 0)
