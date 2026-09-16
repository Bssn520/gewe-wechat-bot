from __future__ import annotations

import inspect

from app.crud import outbox as outbox_crud


def test_peek_next_not_exists_is_scoped_to_self_wxid() -> None:
    """两节点可同时 sending：NOT EXISTS 必须带 x.self_wxid = o.self_wxid。"""
    src = inspect.getsource(outbox_crud.peek_next)
    assert "x.self_wxid = o.self_wxid" in src
    assert "x.status" in src


def test_cas_claim_matches_lease_gen() -> None:
    src = inspect.getsource(outbox_crud.cas_claim_sending)
    assert "lease_gen = $5" in src
    assert "app_id = $6" in src
    assert "COALESCE(sending_at, NOW())" in src


def test_mark_sent_writes_db_now() -> None:
    src = inspect.getsource(outbox_crud.mark_sent)
    assert "sent_at = NOW()" in src
    assert "utcnow" not in src


def test_reclaim_stale_bumps_lease_gen() -> None:
    src = inspect.getsource(outbox_crud.reclaim_stale)
    assert "lease_gen = lease_gen + 1" in src
