from __future__ import annotations

import pytest

from app.core.rate_limit.gates import token_bucket
from app.core.rate_limit.intent import SendIntent


@pytest.fixture(autouse=True)
def _clear_buckets() -> None:
    token_bucket._buckets.clear()
    yield
    token_bucket._buckets.clear()


def test_burst_then_delay(monkeypatch) -> None:
    monkeypatch.setattr("app.core.config.settings.SEND_BURST", 2)
    monkeypatch.setattr("app.core.config.settings.SEND_MAX_PER_MINUTE", 40)
    intent = SendIntent(self_wxid="wx_a", friend_wxid="f1")
    assert token_bucket.delay_seconds(intent) == 0.0
    assert token_bucket.delay_seconds(intent) == 0.0
    delay = token_bucket.delay_seconds(intent)
    assert delay == pytest.approx(1.5, rel=0.05)


def test_buckets_are_per_node(monkeypatch) -> None:
    monkeypatch.setattr("app.core.config.settings.SEND_BURST", 1)
    monkeypatch.setattr("app.core.config.settings.SEND_MAX_PER_MINUTE", 40)
    a = SendIntent(self_wxid="wx_a", friend_wxid="f1")
    b = SendIntent(self_wxid="wx_b", friend_wxid="f1")
    assert token_bucket.delay_seconds(a) == 0.0
    assert token_bucket.delay_seconds(b) == 0.0
    assert token_bucket.delay_seconds(a) > 0
    assert token_bucket.delay_seconds(b) > 0


def test_refill_recovers_after_wait(monkeypatch) -> None:
    monkeypatch.setattr("app.core.config.settings.SEND_BURST", 2)
    monkeypatch.setattr("app.core.config.settings.SEND_MAX_PER_MINUTE", 40)
    clock = {"t": 1000.0}
    monkeypatch.setattr(token_bucket.time, "monotonic", lambda: clock["t"])
    intent = SendIntent(self_wxid="wx_a", friend_wxid="f1")
    assert token_bucket.delay_seconds(intent) == 0.0
    assert token_bucket.delay_seconds(intent) == 0.0
    assert token_bucket.delay_seconds(intent) > 0
    clock["t"] += 3.0
    assert token_bucket.delay_seconds(intent) == 0.0
