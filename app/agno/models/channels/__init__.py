"""渠道与默认模型注册表（改这里切换默认渠道/模型，不进 env）。

新增渠道：
1. 在 `channels/` 新增模块并导出 `CHANNEL: ChannelSpec`
2. 加入下方 `CHANNELS`
3. 按需改 `DEFAULTS`
"""

from __future__ import annotations

from app.agno.models.channels import bailian
from app.agno.models.channels.base import ChannelSpec
from app.agno.models.types import ModelKind

CHANNELS: dict[str, ChannelSpec] = {
    bailian.CHANNEL.id: bailian.CHANNEL,
}

# 各能力默认：(channel_id, model_id)
DEFAULTS: dict[ModelKind, tuple[str, str]] = {
    "vlm": ("bailian", "qwen3.8-flash"),
}

__all__ = ["CHANNELS", "DEFAULTS", "ChannelSpec"]
