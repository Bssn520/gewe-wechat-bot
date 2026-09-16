"""模型能力类型与驱动约定。"""

from __future__ import annotations

from typing import Literal

# 能力分类。一期只注册 vlm（多模态，可纯文本）——图片分析必需
ModelKind = Literal["llm", "vlm"]

# 构建驱动（ChannelSpec.driver → 具体 SDK 适配）
ModelDriver = Literal["openai", "openai_like", "dashscope"]
