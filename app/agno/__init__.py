"""Agno Agent 层：只提供 Agent 生成能力，不挂 AgentOS。

边界纪律：只有 `app/utils/generation.py`（生成接缝）可以 import 本包；
`services` / `crud` / `workers` 一律不得直接引用，否则换框架时会牵动业务层。
"""

from app.agno.agents import (
    DEFAULT_AGENT_ID,
    get_agent,
    resolve_agent_id,
    validate_agent_config,
)

__all__ = [
    "DEFAULT_AGENT_ID",
    "get_agent",
    "resolve_agent_id",
    "validate_agent_config",
]
