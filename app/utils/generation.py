"""生成接缝：`services` 唯一的生成入口。

职责只有一件：**`self_wxid` → agent_id 映射识别 → 取 Agent → 产出文本**。
`services` 不感知「Agent」这一概念；换生成框架只需改本文件与 `app/agno/`。

边界：本模块不得读写数据库、不认识 inbox/outbox/GeWe，只吃数据、只吐文本。
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from agno.run.base import RunStatus

from app.agno import get_agent, resolve_agent_id
from app.agno.models.llm.builders import GENERATION_TIMEOUT_SECONDS
from app.core.gewe_errors import (
    LLM_EMPTY,
    LLM_TIMEOUT,
    LLM_UNKNOWN,
    truncate_error_message,
)

logger = structlog.get_logger(__name__)


class LLMError(Exception):
    def __init__(self, error_code: str, message: str = ""):
        self.error_code = error_code
        self.message = truncate_error_message(message)
        super().__init__(self.message)


def _to_messages(messages: list[dict[str, Any]], images: list[str] | None) -> list[Any]:
    """dict 消息 → agno Message；**剔除 system**；图片挂到最后一条 user 上。

    system 由 Agent 的 instructions 提供（agno 会先生成一条 system 并 prepend）。
    若这里再带一条，会出现两条 system，且我们这条排在框架那条之后——以我们为准
    反而会盖掉人设，故必须剔除。

    图片**必须挂在 user Message 上**，不能走 `arun(images=...)`：当 input 是
    `list[Message]` 时，agno 会跳过「构建 user 消息」那一步，`arun(images=...)`
    会被**静默丢弃**（`agent/_messages.py` step 4 条件不成立）。
    """
    from agno.media import Image
    from agno.models.message import Message

    out: list[Any] = []
    for row in messages:
        role = str(row.get("role") or "")
        if role == "system":
            continue
        out.append(Message(role=role, content=str(row.get("content") or "")))
    if images and out:
        last = out[-1]
        last.images = [Image(url=url) for url in images]
    return out


async def generate_text(
    *,
    self_wxid: str,
    friend_wxid: str,
    messages: list[dict[str, Any]],
    images: list[str] | None = None,
) -> str:
    """按 self_wxid 路由到对应 Agent，生成一条回复文本。失败抛 LLMError。"""
    agent = get_agent(resolve_agent_id(self_wxid))
    run_messages = _to_messages(messages, images)
    # session_id 必须显式传：不传时 agno 会生成 UUID 并回写 agent.session_id
    # （sticky），在缓存实例上造成跨会话污染。无 db 时不落盘，传了无副作用。
    session_id = f"{self_wxid}:{friend_wxid}"

    try:
        run = await asyncio.wait_for(
            agent.arun(run_messages, session_id=session_id),
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise LLMError(LLM_TIMEOUT, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 契约边界：任何异常都译成内部码
        logger.exception("generation.unexpected_error", self_wxid=self_wxid)
        raise LLMError(LLM_UNKNOWN, str(exc)) from exc

    # 非流式 arun 不抛异常：模型报错时返回 status=error 且 content=str(e)。
    # 不查 status 会把错误文本当成回复发出去，故必须显式判。
    if run.status == RunStatus.error:
        text = str(run.content or "")
        logger.warning("generation.run_error", self_wxid=self_wxid, detail=text[:200])
        raise LLMError(LLM_UNKNOWN, text)

    text = str(run.content or "").strip()
    if not text:
        raise LLMError(LLM_EMPTY, "empty content")
    return text
