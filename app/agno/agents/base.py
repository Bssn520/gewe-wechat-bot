"""业务 Agent 的公共基础构建器。

历史所有权在本项目的 `chat_sessions`：
- `db=None`：不持久化 session，历史由编排层组装后传入。
- 不挂 `learning`：它会在初始化时把 `add_history_to_context` 自动置 True，
  从而让 Agno 接管历史。
"""

from __future__ import annotations

from typing import Any

from agno.agent import Agent
from agno.models.base import Model

from app.agno.models import get_chat_model


def build_basic_agent(
    *,
    agent_id: str,
    name: str,
    description: str,
    instructions: Any,
    model: Model | None = None,
) -> Agent:
    """构建 Agent（人设走 instructions；模型默认走渠道 DEFAULTS）。

    模型选型的唯一落点：大多数 Agent 不需要传 ``model``。需要换模型时传
    ``model=get_chat_model(model_id="...")``。
    """
    return Agent(
        id=agent_id,
        name=name,
        model=model or get_chat_model(),
        description=description,
        instructions=instructions,
        db=None,  # 不持久化
        add_history_to_context=False,  # 历史由 chat_sessions 组装后作为 messages 传入
        telemetry=False,  # 必须显式关：agno 默认 True，会往 os-api.agno.com 发送
        retries=0,  # 不叠加重试；inbox 失败即终态
    )


__all__ = ["build_basic_agent"]
