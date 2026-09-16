from __future__ import annotations

import pytest

from app.agno import get_agent, resolve_agent_id, validate_agent_config
from app.agno.agents import AGENTS, DEFAULT_AGENT_ID
from app.agno.models import get_chat_model
from app.core.config import settings


def test_default_agent_is_registered() -> None:
    assert DEFAULT_AGENT_ID in AGENTS
    assert list(AGENTS) == ["reply"]


def test_get_agent_is_cached_per_id() -> None:
    assert get_agent(DEFAULT_AGENT_ID) is get_agent(DEFAULT_AGENT_ID)


def test_get_agent_rejects_unknown_id() -> None:
    with pytest.raises(ValueError, match="未注册的 Agent"):
        get_agent("nope")


def test_resolve_agent_id_falls_back_when_unmapped(monkeypatch) -> None:
    monkeypatch.setattr(settings, "NODE_AGENTS", "")
    assert resolve_agent_id("wx_unknown") == DEFAULT_AGENT_ID


def test_resolve_agent_id_uses_mapping(monkeypatch) -> None:
    monkeypatch.setattr(settings, "NODE_AGENTS", "wx_a:reply,wx_b:reply")
    assert resolve_agent_id("wx_a") == "reply"
    assert resolve_agent_id("wx_b") == "reply"


def test_agents_by_wxid_skips_malformed_chunks(monkeypatch) -> None:
    monkeypatch.setattr(settings, "NODE_AGENTS", "junk,wx_a:reply,:nope,wx_b: , wx_c:reply")
    assert settings.agents_by_wxid == {"wx_a": "reply", "wx_c": "reply"}


def test_validate_agent_config_accepts_clean_config(monkeypatch) -> None:
    monkeypatch.setattr(settings, "NODE_AGENTS", "wx_a:reply")
    validate_agent_config()  # 不抛即通过


def test_validate_agent_config_rejects_unknown_agent(monkeypatch) -> None:
    """拼错 agent_id 必须启动即崩，而不是静默回落到默认人设。"""
    monkeypatch.setattr(settings, "NODE_AGENTS", "wx_a:typo")
    with pytest.raises(ValueError, match="未注册的 Agent"):
        validate_agent_config()


def test_model_factory_uses_project_endpoint_and_thinking() -> None:
    """模型必须打到本项目端点（agno 默认国际站会 401），且思考显式开启。"""
    from app.agno.models.llm.builders import GENERATION_TIMEOUT_SECONDS

    model = get_chat_model()
    assert model.id == "qwen3.8-flash"
    assert model.base_url == settings.DASHSCOPE_BASE_URL
    assert model.enable_thinking is True
    assert GENERATION_TIMEOUT_SECONDS == 300.0
    assert model.timeout == GENERATION_TIMEOUT_SECONDS + 10.0


def test_model_factory_rejects_unknown_model_id() -> None:
    """模型 id 有白名单校验，拼错要在构造期就报错。"""
    with pytest.raises(ValueError, match="未注册"):
        get_chat_model(model_id="qwen-does-not-exist")


def test_default_agent_keeps_history_ownership_with_us() -> None:
    """默认 Agent 必须保持「历史所有权在我们」：不落库、不接管历史。"""
    agent = get_agent(DEFAULT_AGENT_ID)
    assert agent.db is None
    assert agent.add_history_to_context is False
    assert agent.telemetry is False
    assert isinstance(agent.instructions, str)
    assert agent.skills is None
