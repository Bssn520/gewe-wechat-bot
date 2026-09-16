"""聊天模型构建：按渠道 driver 选择具体适配器。

本文件是**模型参数的唯一落点**：模型 id 来自渠道目录，生成参数与超时是这里
的常量（不进 env，避免运维面膨胀）。
"""

from __future__ import annotations

from agno.models.base import Model
from agno.models.dashscope import DashScope
from agno.models.openai import OpenAIChat, OpenAILike

from app.agno.models._common import client_kwargs_for_channel
from app.agno.models.channels.base import ChannelSpec

# --- 生成参数（常量，不进 env）---
MODEL_TEMPERATURE: float = 0.8
# 这是上限不是目标值，对短回复无影响。取值需按目标模型的实际输出能力校准。
MODEL_MAX_TOKENS: int = 4096
# agno 的 DashScope.enable_thinking 默认 False 且每次无条件写 extra_body；
# 必须显式设 True，否则会相对旧实现（不传 = 百炼默认开启）静默关掉思考。
ENABLE_THINKING: bool = True
# 生成硬上限；接缝层用它做 asyncio.wait_for，见 app/utils/generation.py。
# thinking + 看图时上游可能已 200，接缝先切断会让 inbox 进 llm_timeout 终态。
GENERATION_TIMEOUT_SECONDS: float = 300.0
# SDK 层超时留出余量，让接缝的 wait_for 先触发，产出精确的 llm_timeout
_SDK_TIMEOUT_SECONDS: float = GENERATION_TIMEOUT_SECONDS + 10.0


def build_chat_model(*, model_id: str, channel: ChannelSpec) -> Model:
    """按渠道驱动构建聊天模型。"""
    kwargs = client_kwargs_for_channel(channel)
    driver = channel.driver

    if driver == "dashscope":
        # 必须显式传 base_url：agno 默认打到国际站，国内 Key 会 401
        return DashScope(
            id=model_id,
            temperature=MODEL_TEMPERATURE,
            max_tokens=MODEL_MAX_TOKENS,
            timeout=_SDK_TIMEOUT_SECONDS,
            # 关掉 SDK 内置重试，避免与上层语义叠乘；inbox 失败即终态
            max_retries=0,
            enable_thinking=ENABLE_THINKING,
            **kwargs,  # type: ignore[arg-type]  # agno stub 不认 dict 展开，运行时合法
        )
    if driver == "openai":
        return OpenAIChat(id=model_id, **kwargs)  # type: ignore[arg-type]
    if driver == "openai_like":
        return OpenAILike(id=model_id, **kwargs)  # type: ignore[arg-type]
    raise ValueError(f"不支持的聊天驱动: {driver}")


__all__: list[str] = [
    "ENABLE_THINKING",
    "GENERATION_TIMEOUT_SECONDS",
    "MODEL_MAX_TOKENS",
    "MODEL_TEMPERATURE",
    "build_chat_model",
]
