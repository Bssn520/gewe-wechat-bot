"""回调入站。Router 先空 200，再后台跑 handle；失败只打日志。"""

from __future__ import annotations

import asyncio

import structlog

from app.core.config import settings
from app.core.gewe_errors import GEWE_OFFLINE
from app.crud import inbox as inbox_crud
from app.crud import node as node_crud
from app.crud import node_circuit as circuit_crud
from app.crud.node import UpsertOutcome
from app.schemas.webhook import GeWeCallback

logger = structlog.get_logger(__name__)

INSERT_TIMEOUT_S = 5.0
NODE_RETRY_ATTEMPTS = 2
NODE_RETRY_BACKOFF_S = 0.25

SYSTEM_EVENTS = frozenset(
    {
        "LOGOUT",
        "LOGIN_ERROR",
        "LOGIN_SUCCESS",
        "RECONNECT_SUCCESS",
        "RECONNECT_FAIL",
        "LONG_SUCCESS",
        "LONG_FAIL",
        "Long_Serve_Start_Success",
        "Long_Serve_Close",
        "SYSTEM",
    }
)
OFFLINE_EVENTS = frozenset(
    {
        "LOGOUT",
        "LOGIN_ERROR",
        "RECONNECT_FAIL",
        "LONG_FAIL",
        "Long_Serve_Close",
    }
)
ONLINE_EVENTS = frozenset({"LOGIN_SUCCESS", "RECONNECT_SUCCESS", "LONG_SUCCESS"})
VERIFY_CONTENT = "验证回调地址是否可用"
# 一期支持文本与图片；图片源 XML 存 media.source，inbox.content 留空
SUPPORTED_TYPES = frozenset({"TEXT", "IMAGE"})
MEDIA_TYPES = {"IMAGE": "image"}


def _is_group(from_user: str | None, to_user: str | None, content: str | None = None) -> bool:
    """v2 扁平回调的群判定。

    顶层 `fromUser`/`toUser` 含 `@chatroom` 是一种形态；另一种更常见：群标识不在
    顶层，发言人是普通 wxid，`content` 带 `fromUser:\\n` 前缀。只认 `@chatroom`
    会把后一种当成私聊，进而私聊回复该好友。
    """
    if "@chatroom" in (from_user or "") or "@chatroom" in (to_user or ""):
        return True
    sender = from_user or ""
    return bool(sender) and (content or "").startswith(f"{sender}:\n")


def _friend_wxid(*, from_user: str | None, to_user: str | None, self_wxid: str | None) -> str:
    if from_user and from_user != self_wxid:
        return from_user
    return to_user or from_user or ""


async def _ensure_node(*, self_wxid: str, app_id: str) -> bool:
    """建号/换槽。False = 槽已被别的 self_wxid 占用，本条回调该丢。

    瞬时失败（超时/异常）重试一次；仍失败也返回 True，让消息照常落 inbox，节点由
    后续回调补齐——回调已先 ACK 空 200，GeWe 不会重发，丢消息不可接受。
    """
    for attempt in range(1, NODE_RETRY_ATTEMPTS + 1):
        try:
            outcome = await asyncio.wait_for(
                node_crud.upsert(self_wxid=self_wxid, app_id=app_id),
                timeout=INSERT_TIMEOUT_S,
            )
        except TimeoutError:
            logger.error(
                "webhook.node_timeout", app_id=app_id, self_wxid=self_wxid, attempt=attempt
            )
        except Exception:
            logger.exception(
                "webhook.node_failed", app_id=app_id, self_wxid=self_wxid, attempt=attempt
            )
        else:
            if outcome is UpsertOutcome.REJECTED:
                logger.error("webhook.node_rejected", app_id=app_id, self_wxid=self_wxid)
                return False
            return True
        if attempt < NODE_RETRY_ATTEMPTS:
            await asyncio.sleep(NODE_RETRY_BACKOFF_S)
    logger.error("webhook.node_unavailable", app_id=app_id, self_wxid=self_wxid)
    return True


async def _handle_system(*, app_id: str, self_wxid: str, msg_type: str) -> None:
    logger.info("webhook.ignored", reason="system", msg_type=msg_type, app_id=app_id)
    if msg_type in OFFLINE_EVENTS:
        wxid = self_wxid or (await node_crud.get_wxid(app_id) if app_id else None)
        if not wxid:
            return
        opened = await circuit_crud.open_circuit(wxid, reason=GEWE_OFFLINE)
        if opened:
            logger.error("node.offline", self_wxid=wxid, app_id=app_id, msg_type=msg_type)
        return
    if msg_type in ONLINE_EVENTS:
        wxid = self_wxid or (await node_crud.get_wxid(app_id) if app_id else None)
        if not wxid:
            return
        if self_wxid and app_id:
            existing = await node_crud.get_app_id(self_wxid)
            if existing is not None:
                outcome = await node_crud.upsert(self_wxid=self_wxid, app_id=app_id)
                if outcome is UpsertOutcome.REJECTED:
                    logger.error(
                        "webhook.online_slot_rejected",
                        self_wxid=self_wxid,
                        app_id=app_id,
                    )
        await circuit_crud.close_if_expired(wxid)


async def handle_callback_safe(payload: GeWeCallback) -> None:
    """响应发出后由 BackgroundTasks await。异常不能再影响 GeWe。"""
    try:
        await handle_callback(payload)
    except Exception:
        logger.exception("webhook.handle_failed")


async def handle_callback(payload: GeWeCallback) -> None:
    app_id = (payload.appid or "").strip()
    self_wxid = (payload.wxid or "").strip()
    msg_type = (payload.msg_type or "").strip()
    content = payload.content or ""

    if content.strip() == VERIFY_CONTENT:
        logger.info("webhook.ignored", reason="verify")
        return

    if msg_type in SYSTEM_EVENTS:
        await _handle_system(app_id=app_id, self_wxid=self_wxid, msg_type=msg_type)
        return

    if payload.is_self:
        logger.info("webhook.ignored", reason="is_self", app_id=app_id)
        return

    from_user = payload.from_user or ""
    to_user = payload.to_user or ""
    if from_user.startswith("gh_") or to_user.startswith("gh_"):
        logger.info("webhook.ignored", reason="gh", app_id=app_id)
        return

    if _is_group(from_user, to_user, content):
        logger.info("webhook.ignored", reason="group", app_id=app_id)
        return

    if msg_type not in SUPPORTED_TYPES:
        logger.info("webhook.ignored", reason="not_supported", msg_type=msg_type, app_id=app_id)
        return

    new_msg_id = payload.new_msg_id
    if new_msg_id is None or str(new_msg_id).strip() == "":
        logger.warning("webhook.ignored", reason="missing_new_msg_id", app_id=app_id)
        return
    new_msg_id = str(new_msg_id)

    if not app_id:
        logger.warning("webhook.ignored", reason="missing_appid")
        return
    if not self_wxid:
        logger.warning("webhook.ignored", reason="missing_wxid", app_id=app_id)
        return

    friend_wxid = _friend_wxid(from_user=from_user, to_user=to_user, self_wxid=self_wxid)
    if not friend_wxid:
        logger.warning("webhook.ignored", reason="missing_friend", app_id=app_id)
        return

    allow_on = settings.allowlist_enabled
    allow = settings.allowlist_by_wxid
    if allow_on and self_wxid not in allow:
        logger.info("webhook.ignored", reason="unknown_node", app_id=app_id, self_wxid=self_wxid)
        return

    friend_ok = (not allow_on) or (friend_wxid in allow.get(self_wxid, frozenset()))

    if not await _ensure_node(self_wxid=self_wxid, app_id=app_id):
        return

    if not friend_ok:
        logger.info(
            "webhook.ignored",
            reason="not_allowlisted",
            app_id=app_id,
            self_wxid=self_wxid,
            friend_wxid=friend_wxid,
        )
        return

    # 媒体消息：源 XML 进 media.source，inbox.content 留空（占位由 media 派生）
    media_type = MEDIA_TYPES.get(msg_type)

    try:
        row = await asyncio.wait_for(
            inbox_crud.insert_pending(
                self_wxid=self_wxid,
                app_id=app_id,
                friend_wxid=friend_wxid,
                new_msg_id=new_msg_id,
                content=None if media_type else content,
                media_type=media_type,
                media_source={"xml": content} if media_type else None,
            ),
            timeout=INSERT_TIMEOUT_S,
        )
    except TimeoutError:
        logger.error("webhook.inbox_timeout", app_id=app_id, new_msg_id=new_msg_id)
        return
    except Exception:
        logger.exception("webhook.inbox_failed", app_id=app_id, new_msg_id=new_msg_id)
        return

    if row is None:
        logger.info("webhook.duplicate", app_id=app_id, new_msg_id=new_msg_id)
