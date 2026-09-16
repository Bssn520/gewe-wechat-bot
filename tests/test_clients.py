from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from agno.run.base import RunStatus

from app.core.gewe_errors import (
    GEWE_TIMEOUT,
    GEWE_UNAVAILABLE,
    LLM_EMPTY,
    LLM_TIMEOUT,
    LLM_UNKNOWN,
)
from app.utils.generation import LLMError, generate_text
from app.utils.gewe_client import GeWeClient


class _FakeAsyncClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.posts = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers=None, json=None):
        self.posts += 1
        if self._error:
            raise self._error
        return self._response

    async def aclose(self):
        self.closed = True


async def test_gewe_post_text_success(monkeypatch) -> None:
    resp = SimpleNamespace(
        status_code=200,
        text="",
        json=lambda: {"ret": 200, "msg": "ok", "data": {"newMsgId": 77}},
    )
    monkeypatch.setattr(
        "app.utils.gewe_client.httpx.AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response=resp),
    )
    result = await GeWeClient().post_text(app_id="wx_a", to_wxid="f1", content="hi")
    assert result.ok
    assert result.new_msg_id == "77"


async def test_gewe_post_text_reuses_one_client(monkeypatch) -> None:
    resp = SimpleNamespace(
        status_code=200,
        text="",
        json=lambda: {"ret": 200, "msg": "ok", "data": {"newMsgId": 1}},
    )
    made: list[_FakeAsyncClient] = []

    def _factory(**kwargs):
        fake = _FakeAsyncClient(response=resp)
        made.append(fake)
        return fake

    monkeypatch.setattr("app.utils.gewe_client.httpx.AsyncClient", _factory)
    client = GeWeClient()
    await client.post_text(app_id="wx_a", to_wxid="f1", content="a")
    await client.post_text(app_id="wx_a", to_wxid="f2", content="b")
    assert len(made) == 1
    assert made[0].posts == 2
    await client.aclose()
    assert made[0].closed


async def test_gewe_post_text_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.utils.gewe_client.httpx.AsyncClient",
        lambda **kwargs: _FakeAsyncClient(error=httpx.TimeoutException("slow")),
    )
    result = await GeWeClient().post_text(app_id="wx_a", to_wxid="f1", content="hi")
    assert result.error_code == GEWE_TIMEOUT


async def test_gewe_post_text_transport(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.utils.gewe_client.httpx.AsyncClient",
        lambda **kwargs: _FakeAsyncClient(error=httpx.ConnectError("down")),
    )
    result = await GeWeClient().post_text(app_id="wx_a", to_wxid="f1", content="hi")
    assert result.error_code == GEWE_UNAVAILABLE


class _FakeAgent:
    """假 Agent：arun 返回带 status/content 的 RunOutput 形状。"""

    def __init__(self, *, content=None, status=None, error: Exception | None = None) -> None:
        self._content = content
        self._status = status or RunStatus.completed
        self._error = error
        self.calls: list[dict] = []

    async def arun(self, messages, *, session_id=None):
        self.calls.append({"messages": messages, "session_id": session_id})
        if self._error is not None:
            raise self._error
        return SimpleNamespace(status=self._status, content=self._content)


def _patch_agent(monkeypatch, agent: _FakeAgent) -> None:
    monkeypatch.setattr("app.utils.generation.get_agent", lambda agent_id: agent)


async def test_generate_text_strips_content(monkeypatch) -> None:
    agent = _FakeAgent(content="  你好  ")
    _patch_agent(monkeypatch, agent)
    text = await generate_text(
        self_wxid="wx_a", friend_wxid="f1", messages=[{"role": "user", "content": "hi"}]
    )
    assert text == "你好"


async def test_generate_text_drops_system_and_passes_session_id(monkeypatch) -> None:
    agent = _FakeAgent(content="回")
    _patch_agent(monkeypatch, agent)
    await generate_text(
        self_wxid="wx_a",
        friend_wxid="f1",
        messages=[
            {"role": "system", "content": "人设"},
            {"role": "user", "content": "hi"},
        ],
    )
    call = agent.calls[0]
    assert call["session_id"] == "wx_a:f1"
    assert all(m.role != "system" for m in call["messages"])


async def test_generate_text_empty_raises(monkeypatch) -> None:
    _patch_agent(monkeypatch, _FakeAgent(content="  "))
    with pytest.raises(LLMError) as exc:
        await generate_text(
            self_wxid="wx_a", friend_wxid="f1", messages=[{"role": "user", "content": "hi"}]
        )
    assert exc.value.error_code == LLM_EMPTY


async def test_generate_text_run_error_maps_to_unknown(monkeypatch) -> None:
    """非流式 arun 不抛异常：status=error 必须被识别，否则错误文本会被当回复发出。"""
    _patch_agent(
        monkeypatch, _FakeAgent(content="Error code: 429 - rate limit", status=RunStatus.error)
    )
    with pytest.raises(LLMError) as exc:
        await generate_text(
            self_wxid="wx_a", friend_wxid="f1", messages=[{"role": "user", "content": "hi"}]
        )
    assert exc.value.error_code == LLM_UNKNOWN


async def test_generate_text_timeout_raises(monkeypatch) -> None:
    _patch_agent(monkeypatch, _FakeAgent(error=TimeoutError("slow")))
    with pytest.raises(LLMError) as exc:
        await generate_text(
            self_wxid="wx_a", friend_wxid="f1", messages=[{"role": "user", "content": "hi"}]
        )
    assert exc.value.error_code == LLM_TIMEOUT
