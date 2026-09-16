from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.core.rate_limit.domain import RateDomain
from app.core.rate_limit.gates import backlog, circuit, daily_quota, distinct_friends, interval
from app.core.rate_limit.intent import SendIntent
from app.core.rate_limit.plan import RatePlan
from app.core.rate_limit.registry import for_account
from app.crud.base import as_utc

INTENT = SendIntent(self_wxid="wx_a", friend_wxid="f1")


def _window(*friends: str, ttl: float = 30.0) -> list[tuple[str, float]]:
    return [(friend, ttl) for friend in friends]


async def test_distinct_friends_same_friend_immediate() -> None:
    with patch(
        "app.core.rate_limit.gates.distinct_friends.outbox_crud.distinct_friends_window",
        new=AsyncMock(return_value=_window("f1", "f2", ttl=40.0)),
    ):
        assert await distinct_friends.delay_seconds(INTENT) == 0.0


async def test_distinct_friends_new_under_cap_immediate(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.core.rate_limit.gates.distinct_friends.settings.SEND_MAX_DISTINCT_FRIENDS_PER_MINUTE",
        10,
    )
    with patch(
        "app.core.rate_limit.gates.distinct_friends.outbox_crud.distinct_friends_window",
        new=AsyncMock(return_value=_window(*[f"u{i}" for i in range(9)])),
    ):
        assert await distinct_friends.delay_seconds(INTENT) == 0.0


async def test_distinct_friends_new_when_full_waits_oldest(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.core.rate_limit.gates.distinct_friends.settings.SEND_MAX_DISTINCT_FRIENDS_PER_MINUTE",
        10,
    )
    window = [(f"u{i}", 40.0) for i in range(10)]
    window[3] = ("u3", 7.5)
    with patch(
        "app.core.rate_limit.gates.distinct_friends.outbox_crud.distinct_friends_window",
        new=AsyncMock(return_value=window),
    ):
        assert await distinct_friends.delay_seconds(INTENT) == 7.5


async def test_circuit_admit() -> None:
    with patch(
        "app.core.rate_limit.gates.circuit.circuit_crud.is_open", new=AsyncMock(return_value=True)
    ):
        assert await circuit.admit(INTENT) is False
    with patch(
        "app.core.rate_limit.gates.circuit.circuit_crud.is_open", new=AsyncMock(return_value=False)
    ):
        assert await circuit.admit(INTENT) is True


async def test_daily_quota_and_backlog(monkeypatch) -> None:
    monkeypatch.setattr("app.core.rate_limit.gates.daily_quota.settings.SEND_DAILY_CAP", 2)
    monkeypatch.setattr("app.core.rate_limit.gates.backlog.settings.OUTBOX_BACKLOG_PER_ACCOUNT", 2)
    with patch(
        "app.core.rate_limit.gates.daily_quota.outbox_crud.sent_count_today",
        new=AsyncMock(return_value=2),
    ):
        assert await daily_quota.admit(INTENT) is False
    with patch(
        "app.core.rate_limit.gates.daily_quota.outbox_crud.sent_count_today",
        new=AsyncMock(return_value=1),
    ):
        assert await daily_quota.admit(INTENT) is True
    with patch(
        "app.core.rate_limit.gates.backlog.outbox_crud.pending_count",
        new=AsyncMock(return_value=2),
    ):
        assert await backlog.admit(INTENT) is False
    with patch(
        "app.core.rate_limit.gates.backlog.outbox_crud.pending_count",
        new=AsyncMock(return_value=1),
    ):
        assert await backlog.admit(INTENT) is True


async def test_interval_none_last_sent() -> None:
    with patch(
        "app.core.rate_limit.gates.interval.outbox_crud.last_sent", new=AsyncMock(return_value=None)
    ):
        assert await interval.delay_seconds(INTENT) == 0.0


async def test_interval_same_friend_respects_elapsed(monkeypatch) -> None:
    monkeypatch.setattr("app.core.rate_limit.gates.interval.random.uniform", lambda a, b: 1.5)
    with patch(
        "app.core.rate_limit.gates.interval.outbox_crud.last_sent",
        new=AsyncMock(return_value=("f1", 0.5)),
    ):
        delay = await interval.delay_seconds(INTENT)
    assert delay == pytest.approx(1.0, abs=0.2)


def test_as_utc_treats_naive_as_utc() -> None:
    naive = datetime(2026, 9, 12, 2, 20, 46)
    aware = datetime(2026, 9, 12, 2, 20, 46, tzinfo=UTC)
    shanghai = datetime(2026, 9, 12, 10, 20, 46, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert as_utc(naive) == aware
    assert as_utc(aware) == aware
    assert as_utc(shanghai) == aware


async def test_plan_circuit_ok_matches_gate() -> None:
    plan = RatePlan()
    with patch.object(circuit, "admit", new=AsyncMock(return_value=False)):
        assert await plan.circuit_ok(INTENT) is False
    with patch.object(circuit, "admit", new=AsyncMock(return_value=True)):
        assert await plan.circuit_ok(INTENT) is True


async def test_plan_admit_short_circuits() -> None:
    plan = RatePlan()
    with (
        patch.object(circuit, "admit", new=AsyncMock(return_value=False)),
        patch.object(daily_quota, "admit", new=AsyncMock()) as quota,
        patch.object(backlog, "admit", new=AsyncMock()) as back,
    ):
        assert await plan.admit(INTENT) is False
    quota.assert_not_called()
    back.assert_not_called()


async def test_wait_to_dispatch_takes_max() -> None:
    plan = RatePlan()
    with (
        patch.object(distinct_friends, "delay_seconds", new=AsyncMock(return_value=2.0)),
        patch("app.core.rate_limit.plan.token_bucket.delay_seconds", return_value=0.5),
        patch.object(interval, "delay_seconds", new=AsyncMock(return_value=1.0)),
    ):
        assert await plan.wait_to_dispatch(INTENT) == 2.0


def test_for_account_rejects_non_outbound() -> None:
    for_account("wx_a", RateDomain.OUTBOUND_TEXT)
    with pytest.raises(ValueError, match="只支持"):
        for_account("wx_a", "download")  # type: ignore[arg-type]


def test_send_intent_defaults_domain() -> None:
    assert INTENT.domain is RateDomain.OUTBOUND_TEXT
