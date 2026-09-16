from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import TORTOISE_ORM, Settings, settings

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_FILES = (
    ".env.example",
    ".env.dev.example",
    ".env.test.example",
    ".env.prod.example",
)


def _env_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        keys.add(raw.split("=", 1)[0].strip())
    return keys


def test_allowlist_empty_disables_feature(monkeypatch) -> None:
    monkeypatch.setattr(settings, "GEWE_ALLOWLIST", "")
    assert settings.allowlist_enabled is False
    assert settings.allowlist_by_wxid == {}


def test_allowlist_whitespace_only_disables_feature(monkeypatch) -> None:
    monkeypatch.setattr(settings, "GEWE_ALLOWLIST", "   ")
    assert settings.allowlist_enabled is False
    assert settings.allowlist_by_wxid == {}


def test_allowlist_parses_self_wxid_to_friends(monkeypatch) -> None:
    monkeypatch.setattr(settings, "GEWE_ALLOWLIST", "wx_a:f1|f2,wx_b:f3")
    assert settings.allowlist_enabled is True
    assert settings.allowlist_by_wxid == {
        "wx_a": frozenset({"f1", "f2"}),
        "wx_b": frozenset({"f3"}),
    }


def test_allowlist_skips_chunks_without_colon_or_empty_friends(monkeypatch) -> None:
    monkeypatch.setattr(settings, "GEWE_ALLOWLIST", "junk,wx_a:,:f1, wx_b: f2 | ")
    assert settings.allowlist_by_wxid == {"wx_b": frozenset({"f2"})}


def test_prod_rejects_debug_true() -> None:
    s = Settings.model_construct(APP_ENV="prod", DEBUG=True)
    with pytest.raises(ValueError, match="prod 禁止 DEBUG=true"):
        Settings._prod_must_not_debug(s)


def test_prod_requires_secrets() -> None:
    s = Settings.model_construct(
        APP_ENV="prod",
        DEBUG=False,
        DASHSCOPE_API_KEY="",
        GEWE_TOKEN="",
        WEBHOOK_SECRET="",
        DB_PASSWORD="",
    )
    with pytest.raises(ValueError, match="缺少"):
        Settings._prod_requires_secrets(s)


def test_tortoise_orm_pins_aware_utc() -> None:
    assert TORTOISE_ORM["use_tz"] is True
    assert TORTOISE_ORM["timezone"] == "UTC"


def test_chat_store_rounds_defaults_above_reply_history() -> None:
    assert settings.CHAT_STORE_ROUNDS == 200
    assert settings.REPLY_HISTORY_ROUNDS == 20


def test_inbox_lease_covers_generation_timeout() -> None:
    """租约必须盖过生成硬上限 + 写库余量，否则 LLM 还在跑就会被回收重认领。"""
    from app.agno.models.llm.builders import GENERATION_TIMEOUT_SECONDS

    assert int(GENERATION_TIMEOUT_SECONDS) + 60 <= settings.INBOX_LEASE_SECONDS


def test_env_example_lists_every_settings_field() -> None:
    """`.env.example` 是字段清单：Settings 有任何字段它都必须有，防止新增配置漏文档。"""
    fields = set(Settings.model_fields)
    keys = _env_keys(REPO_ROOT / ".env.example")
    assert fields - keys == set(), f".env.example 缺少字段: {sorted(fields - keys)}"
    extra = keys - fields
    assert extra == set(), f".env.example 有未知键（拼写错/已废弃）: {sorted(extra)}"


def test_all_env_examples_share_same_key_set() -> None:
    """各环境骨架必须与字段清单同键集，避免某个环境漏配。"""
    base = _env_keys(REPO_ROOT / EXAMPLE_FILES[0])
    for name in EXAMPLE_FILES[1:]:
        keys = _env_keys(REPO_ROOT / name)
        assert keys == base, (
            f"{name} 与 {EXAMPLE_FILES[0]} 键集不一致: "
            f"缺 {sorted(base - keys)}, 多 {sorted(keys - base)}"
        )
