from __future__ import annotations

from app.crud.chat_session import delivered_window


def test_delivered_window_empty_returns_empty() -> None:
    assert delivered_window([]) == []


def test_delivered_window_skips_undelivered_assistant() -> None:
    elements = [
        {"role": "assistant", "content": "ghost", "delivered": False},
        {"role": "user", "content": "hi", "delivered": True},
    ]
    rows = delivered_window(elements)
    assert [row["content"] for row in rows] == ["hi"]


def test_delivered_window_caps_to_reply_history_rounds(monkeypatch) -> None:
    monkeypatch.setattr("app.crud.chat_session.settings.REPLY_HISTORY_ROUNDS", 1)
    elements = [
        {"role": "assistant", "content": "a3", "delivered": True},
        {"role": "user", "content": "u3", "delivered": True},
        {"role": "assistant", "content": "a2", "delivered": True},
        {"role": "user", "content": "u2", "delivered": True},
        {"role": "assistant", "content": "a1", "delivered": True},
        {"role": "user", "content": "u1", "delivered": True},
    ]
    rows = delivered_window(elements)
    assert [row["content"] for row in rows] == ["u3", "a3"]
