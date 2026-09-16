"""生成编排：准入 → 按会话认领 → LLM → 同一事务写历史 + outbox + inbox done。"""

from __future__ import annotations

import asyncio

import structlog
from tortoise.transactions import in_transaction

from app.core.config import settings
from app.core.queue_constants import ROLE_ASSISTANT
from app.core.rate_limit import RateDomain, SendIntent, for_account
from app.crud import chat_session as chat_crud
from app.crud import inbox as inbox_crud
from app.crud import media as media_crud
from app.crud import outbox as outbox_crud
from app.models.media import MEDIA_IMAGE, MEDIA_READY
from app.services import media as media_service
from app.utils.generation import LLMError, generate_text
from app.utils.text_split import split_for_wechat

logger = structlog.get_logger(__name__)


def _pick_images(batch: list, media_by_inbox: dict) -> list[str]:
    """本批已就绪的图片引用，最多 MEDIA_MAX_PER_TURN 张（取最新）。

    超出部分不静默丢弃：由 `render` 的文本占位体现，另在批文本里补一句说明，
    避免模型以为「只有这几张」。
    """
    ready = [
        m.asset_ref
        for row in batch
        for m in [media_by_inbox.get(row.id)]
        if m is not None and m.media_type == MEDIA_IMAGE and m.status == MEDIA_READY and m.asset_ref
    ]
    return ready[-media_service.MEDIA_MAX_PER_TURN :]


class StaleLeaseError(Exception):
    """回写时租约代次对不上：行已被回收/接管。触发事务回滚，避免重复回复。"""

    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(f"inbox lease taken over: expected {expected} rows, wrote {actual}")


def assemble_messages(
    history: list[dict[str, object]], batch_texts: list[str]
) -> list[dict[str, str]]:
    """组装给生成层的消息序列。

    不产生 system 条目：人设由 Agent 的 instructions 提供（见 app/agno/agents/）。
    """
    pairs = list(history)
    while pairs and pairs[0].get("role") == ROLE_ASSISTANT:
        pairs = pairs[1:]
    while len(pairs) >= 2 and pairs[-1].get("role") != ROLE_ASSISTANT:
        pairs = pairs[:-1]

    messages: list[dict[str, str]] = []
    for row in pairs:
        role = "assistant" if row.get("role") == ROLE_ASSISTANT else "user"
        messages.append({"role": role, "content": str(row.get("content") or "")})
    messages.append({"role": "user", "content": "\n".join(batch_texts)})
    return messages


async def pick_next_session(
    *,
    exclude_sessions: set[tuple[str, str]] | frozenset[tuple[str, str]] | None = None,
    exclude_wxids: set[str] | frozenset[str] | None = None,
) -> tuple[str, str] | None:
    """准入通过的下一个会话。未过的号本轮跳过，不睡。"""
    skipped: set[str] = set(exclude_wxids or ())
    in_flight = exclude_sessions or set()
    while True:
        peeked = await inbox_crud.peek_next_session(
            exclude_wxids=skipped, exclude_sessions=in_flight
        )
        if peeked is None:
            return None
        self_wxid, friend_wxid = peeked
        plan = for_account(self_wxid, RateDomain.OUTBOUND_TEXT)
        intent = SendIntent(self_wxid=self_wxid, friend_wxid=friend_wxid)
        if not await plan.admit(intent):
            logger.info("reply.admit_denied", self_wxid=self_wxid, friend_wxid=friend_wxid)
            skipped.add(self_wxid)
            continue
        return self_wxid, friend_wxid


async def process_session(worker_id: str, self_wxid: str, friend_wxid: str) -> bool:
    """认领并处理一个会话批次。True 表示本轮有活干（含 claim 落空）。"""
    batch = await inbox_crud.claim_session_batch(
        worker_id=worker_id, self_wxid=self_wxid, friend_wxid=friend_wxid
    )
    if not batch:
        return True

    claimed_at = batch[0].claimed_at
    created_ats = [row.created_at for row in batch if row.created_at is not None]
    oldest_created = min(created_ats) if created_ats else None
    debounce_s = settings.DEBOUNCE_MS / 1000.0
    if settings.DEBOUNCE_MS > 0 and created_ats:
        # 滑窗：右界钉在当前批最后一条。新消息只要与上一条间隔 ≤ DEBOUNCE_MS
        # 就并入，并重新从这条再等一轮；中间裂开超过窗宽才切断。
        while len(batch) < settings.INBOX_BATCH_MAX:
            last_created = max(row.created_at for row in batch if row.created_at is not None)
            window_end = inbox_crud.debounce_window_end(last_created, settings.DEBOUNCE_MS)
            remaining = await inbox_crud.seconds_until(window_end)
            if remaining > 0:
                await asyncio.sleep(remaining)
            extra = await inbox_crud.claim_more_same_session(
                self_wxid=self_wxid,
                friend_wxid=friend_wxid,
                worker_id=worker_id,
                after=last_created,
                limit=settings.INBOX_BATCH_MAX - len(batch),
            )
            if not extra:
                break
            batch.extend(extra)

    batch.sort(key=lambda r: r.id)
    ids = [row.id for row in batch]
    gens = [row.lease_gen for row in batch]
    queue_s = None
    if claimed_at is not None and oldest_created is not None:
        queue_s = round((claimed_at - oldest_created).total_seconds(), 3)

    try:
        history = await chat_crud.recent_delivered(self_wxid=self_wxid, friend_wxid=friend_wxid)
        # 媒体消息的 content 是 NULL，其文本表示由 media 派生（render）。
        # 原始 XML 从未进过 content，故不可能泄漏进提示词或会话 JSON。
        media_by_inbox = {m.inbox_id: m for m in await media_crud.by_inbox_ids(ids)}
        texts = [
            (row.content or "")
            if row.id not in media_by_inbox
            else media_service.render(media_by_inbox[row.id])
            for row in batch
        ]
        batch_text = "\n".join(t for t in texts if t)
        images = _pick_images(batch, media_by_inbox)
        ready_count = sum(
            1
            for row in batch
            if (m := media_by_inbox.get(row.id)) is not None
            and m.media_type == MEDIA_IMAGE
            and m.status == MEDIA_READY
            and m.asset_ref
        )
        if ready_count > len(images):
            # 超出单轮上限：明确告知还有图没展示，避免模型以为只有这几张
            batch_text += f"\n（对方还发了 {ready_count - len(images)} 张图未展示）"
        messages = assemble_messages(history, [batch_text])
        text = await generate_text(
            self_wxid=self_wxid,
            friend_wxid=friend_wxid,
            messages=messages,
            images=images,
        )
    except LLMError as exc:
        await inbox_crud.mark_batch_failed(
            ids, gens, error_code=exc.error_code, error_message=exc.message
        )
        logger.warning("reply.llm_failed", self_wxid=self_wxid, error_code=exc.error_code)
        return True
    except Exception as exc:
        await inbox_crud.mark_batch_failed(
            ids, gens, error_code="llm_unknown", error_message=str(exc)[:500]
        )
        logger.exception("reply.llm_unknown", self_wxid=self_wxid)
        return True

    # 长报告拆条：一条生成结果可能超过微信单条长度上限，拆成 N 条 outbox 行。
    # 拆分只在**投递**这一侧发生——历史里存的仍是完整文本，这样 7 天后追问
    # 细节时模型仍读得到报告全文（这是不做图片回挂的前提）。
    parts = split_for_wechat(text)

    try:
        async with in_transaction():
            await chat_crud.append_turn(
                self_wxid=self_wxid,
                friend_wxid=friend_wxid,
                user_content="\n".join(texts),
                assistant_content=text,
            )
            # N 行同事务插入 → id 连续，peek_next 按 id 升序发送，段落顺序正确。
            # outbox 没有 (self_wxid, friend_wxid) 唯一约束，多条 pending 合法。
            for part in parts:
                await outbox_crud.create_pending(
                    self_wxid=self_wxid,
                    friend_wxid=friend_wxid,
                    content=part,
                    source_inbox_ids=ids,
                )
            done = await inbox_crud.mark_batch_done(ids, gens)
            if done != len(ids):
                # 租约已被回收/接管：整批回滚，交给新持有者重新生成，避免重复回复。
                raise StaleLeaseError(len(ids), done)
    except StaleLeaseError as exc:
        logger.warning(
            "reply.stale_lease",
            self_wxid=self_wxid,
            friend_wxid=friend_wxid,
            inbox_ids=ids,
            expected=exc.expected,
            actual=exc.actual,
        )
        return True

    logger.info(
        "reply.generated",
        self_wxid=self_wxid,
        friend_wxid=friend_wxid,
        inbox_ids=ids,
        queue_s=queue_s,
        debounce_s=debounce_s if settings.DEBOUNCE_MS > 0 else 0.0,
    )
    return True


async def process_one_batch(worker_id: str) -> bool:
    """认领并处理一个会话批次。True 表示本轮有活干。"""
    peeked = await pick_next_session()
    if peeked is None:
        return False
    self_wxid, friend_wxid = peeked
    return await process_session(worker_id, self_wxid, friend_wxid)
