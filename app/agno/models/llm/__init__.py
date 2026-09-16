"""聊天模型工厂。

一期只有一种能力（vlm，多模态可纯文本），故不拆 get_llm_model / get_vlm_model
两个入口——需要纯文本模型时给渠道目录加 `llm` 一项即可。

驱动约定：`builders.py` 按 channel.driver 选实现，driver="dashscope" 走官方
agno `DashScope`（不写包装子类）。
"""

from __future__ import annotations

from agno.models.base import Model

from app.agno.models.llm.builders import build_chat_model
from app.agno.models.registry import resolve_model


def get_chat_model(*, model_id: str | None = None) -> Model:
    """获取聊天模型（DEFAULTS["vlm"]）。"""
    channel, resolved_id = resolve_model("vlm", model_id=model_id)
    return build_chat_model(model_id=resolved_id, channel=channel)


__all__ = ["get_chat_model"]
