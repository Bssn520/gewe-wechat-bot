"""Agent 模型层：渠道目录 + 聊天模型工厂。

用法::

    from app.agno.models import get_chat_model
    Agent(model=get_chat_model())

模型 id 只声明在 `channels/` 里；生成参数是 `llm/builders.py` 的常量。
"""

from app.agno.models.channels import CHANNELS, DEFAULTS, ChannelSpec
from app.agno.models.llm import get_chat_model
from app.agno.models.registry import get_channel, resolve_model
from app.agno.models.types import ModelDriver, ModelKind

__all__ = [
    "CHANNELS",
    "DEFAULTS",
    "ChannelSpec",
    "ModelDriver",
    "ModelKind",
    "get_channel",
    "get_chat_model",
    "resolve_model",
]
