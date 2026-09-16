"""发送编排：peek → 发放等待 → CAS 认领 → 立刻 postText → fencing 回写。"""

from __future__ import annotations

import asyncio
import time

import structlog

from app.core.config import settings
from app.core.gewe_errors import SEND_CIRCUIT_GLOBAL, SEND_CIRCUIT_NODE, SEND_RETRY_ONCE
from app.core.queue_constants import OUTBOX_SENDING
from app.core.rate_limit import RateDomain, SendIntent, for_account
from app.crud import chat_session as chat_crud
from app.crud import node as node_crud
from app.crud import node_circuit as circuit_crud
from app.crud import outbox as outbox_crud
from app.utils.gewe_client import gewe_client

logger = structlog.get_logger(__name__)


def typing_delay_s(text: str) -> float:
    return min(5.0, max(0.8, len(text) * 0.04))


async def process_one(self_wxid: str) -> bool:
    """处理该节点一条待发。True 表示本轮有活（含 sleep）。"""
    peeked = await outbox_crud.peek_next(self_wxid)
    if peeked is None:
        return False

    app_id = await node_crud.get_app_id(self_wxid)
    if not app_id:
        logger.error("outbound.missing_slot", self_wxid=self_wxid, outbox_id=peeked.id)
        return False

    intent = SendIntent(self_wxid=self_wxid, friend_wxid=peeked.friend_wxid)
    plan = for_account(self_wxid, RateDomain.OUTBOUND_TEXT)
    if not await plan.circuit_ok(intent):
        logger.info("outbound.circuit_open", self_wxid=self_wxid, outbox_id=peeked.id)
        return False

    delay = await plan.wait_to_dispatch(intent)
    delay = max(delay, typing_delay_s(peeked.content))
    if delay > 0:
        await asyncio.sleep(delay)

    if not await plan.circuit_ok(intent):
        logger.info("outbound.circuit_open", self_wxid=self_wxid, outbox_id=peeked.id)
        return False

    claimed = await outbox_crud.cas_claim_sending(peeked.id, peeked.lease_gen, app_id=app_id)
    if claimed is None:
        logger.info("outbound.claim_miss", outbox_id=peeked.id, self_wxid=self_wxid)
        return True

    t0 = time.monotonic()
    result = await gewe_client.post_text(
        app_id=app_id,
        to_wxid=claimed.friend_wxid,
        content=claimed.content,
    )
    http_s = round(time.monotonic() - t0, 3)
    match = {"status": OUTBOX_SENDING, "lease_gen": claimed.lease_gen}

    if result.ok:
        rows = await outbox_crud.mark_sent(
            claimed.id, lease_gen=claimed.lease_gen, external_id=result.new_msg_id
        )
        if rows == 0:
            logger.error("outbox.stale_lease", outbox_id=claimed.id, lease_gen=claimed.lease_gen)
            return True
        logger.info(
            "outbound.sent",
            self_wxid=self_wxid,
            outbox_id=claimed.id,
            http_s=http_s,
        )
        await _safe_mark_delivered(self_wxid, claimed.friend_wxid)
        return True

    code = result.error_code or "gewe_unknown"
    if code in SEND_RETRY_ONCE and claimed.attempt < settings.SEND_MAX_ATTEMPTS:
        await outbox_crud.schedule_retry(
            claimed.id,
            lease_gen=claimed.lease_gen,
            error_code=code,
            error_message=result.error_message,
            delay_s=2.0,
        )
        return True

    await outbox_crud.mark_failed(
        claimed.id,
        match=match,
        error_code=code,
        error_message=result.error_message,
    )
    if code in SEND_CIRCUIT_NODE:
        await circuit_crud.open_circuit(self_wxid, reason=code)
        logger.error("node.circuit", self_wxid=self_wxid, error_code=code)
    if code in SEND_CIRCUIT_GLOBAL:
        logger.error("gewe.auth_failed", self_wxid=self_wxid, error_code=code)
    return True


async def _safe_mark_delivered(self_wxid: str, friend_wxid: str) -> None:
    try:
        await chat_crud.mark_last_assistant_delivered(self_wxid=self_wxid, friend_wxid=friend_wxid)
    except Exception:
        logger.exception(
            "outbox.post_sent_hook_failed", self_wxid=self_wxid, friend_wxid=friend_wxid
        )
