from __future__ import annotations

from app.services.reply import assemble_messages


def test_assemble_messages_empty_history_appends_joined_batch() -> None:
    msgs = assemble_messages([], ["一", "二", "三"])
    # 人设由 Agent 的 instructions 提供，这里不产生 system 条目
    assert all(m["role"] != "system" for m in msgs)
    assert msgs == [{"role": "user", "content": "一\n二\n三"}]


def test_assemble_messages_includes_history_content() -> None:
    history = [
        {"role": "user", "content": "hi", "delivered": True},
        {"role": "assistant", "content": "hello", "delivered": True},
    ]
    msgs = assemble_messages(history, ["新消息"])
    assert all(m["role"] != "system" for m in msgs)
    assert any(m["content"] == "hello" for m in msgs)
    assert msgs[-1] == {"role": "user", "content": "新消息"}


def test_assemble_messages_does_not_drop_undelivered_assistant() -> None:
    history = [
        {"role": "user", "content": "hi", "delivered": True},
        {"role": "assistant", "content": "ghost", "delivered": False},
    ]
    msgs = assemble_messages(history, ["新"])
    assert any(m["content"] == "ghost" for m in msgs)


def test_assemble_messages_drops_leading_assistant_and_trailing_unpaired_user() -> None:
    history = [
        {"role": "assistant", "content": "orphan"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "dangling"},
    ]
    msgs = assemble_messages(history, ["新"])
    contents = [m["content"] for m in msgs]
    assert "orphan" not in contents
    assert "dangling" not in contents
    assert contents[-2:] == ["a1", "新"]
