"""Agent 定义与统一注册。

新增 Agent 的完整步骤（不需要改 services / 队列）：
1. 在 `agents/` 下新增一个模块（照 `reply.py` 的形态）：AGENT_ID / NAME /
   DESCRIPTION / INSTRUCTIONS / build_xxx_agent
2. 在下方 `AGENTS` 注册表加一行
3. 在 env 的 `NODE_AGENTS` 里配 `wxid:agent_id`（可选，未配则回落默认）
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from agno.agent import Agent

from app.agno.agents.reply import build_reply_agent
from app.core.config import settings

logger = structlog.get_logger(__name__)

# 未在任何映射中命中的 self_wxid 回落到这个 Agent
DEFAULT_AGENT_ID = "reply"

# agent_id → 工厂。新增 Agent 加一行即可
AGENTS: dict[str, Callable[[], Agent]] = {
    "reply": build_reply_agent,
}

# 按 agent_id 缓存的实例：保住 model 的 httpx 连接复用，避免每轮重建
_CACHE: dict[str, Agent] = {}


def validate_agent_config() -> None:
    """校验映射配置只引用已注册的 Agent；不合法直接抛错。

    必须在 worker 启动时调用（fail fast）：`NODE_AGENTS` 里拼错一个 id 时，
    静默回落到默认 Agent 会「上线错人设」——比直接崩溃更糟。

    注意：这里刻意不写成 Settings 的 model_validator，否则 app.agno → app.core.config
    的依赖会绕成循环导入。校验必须在部署层显式调用一次。
    """
    if DEFAULT_AGENT_ID not in AGENTS:
        raise ValueError(f"DEFAULT_AGENT_ID={DEFAULT_AGENT_ID!r} 未注册；已注册: {sorted(AGENTS)}")
    unknown = {
        agent_id: wxid
        for wxid, agent_id in settings.agents_by_wxid.items()
        if agent_id not in AGENTS
    }
    if unknown:
        detail = ", ".join(f"{wxid}→{agent_id}" for wxid, agent_id in sorted(unknown.items()))
        raise ValueError(f"NODE_AGENTS 引用了未注册的 Agent（{detail}）；已注册: {sorted(AGENTS)}")


def resolve_agent_id(self_wxid: str) -> str:
    """self_wxid → agent_id。未命中映射则回落默认，并记 info 便于发现漏配。"""
    agent_id = settings.agents_by_wxid.get(self_wxid)
    if agent_id is not None:
        return agent_id
    logger.info("agent.default_applied", self_wxid=self_wxid, agent_id=DEFAULT_AGENT_ID)
    return DEFAULT_AGENT_ID


def get_agent(agent_id: str) -> Agent:
    """取（并缓存）Agent 实例。"""
    cached = _CACHE.get(agent_id)
    if cached is not None:
        return cached
    factory = AGENTS.get(agent_id)
    if factory is None:
        raise ValueError(f"未注册的 Agent: {agent_id!r}；已注册: {sorted(AGENTS)}")
    agent = factory()
    _CACHE[agent_id] = agent
    return agent


__all__ = [
    "AGENTS",
    "DEFAULT_AGENT_ID",
    "get_agent",
    "resolve_agent_id",
    "validate_agent_config",
]
