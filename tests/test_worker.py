from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.workers import reply as worker


async def test_supervise_starts_loop_for_new_wxid(monkeypatch) -> None:
    monkeypatch.setattr(worker.settings, "WORKER_POLL_SECONDS", 0.01)
    stop = asyncio.Event()
    running: dict[str, asyncio.Task] = {}
    started = asyncio.Event()

    async def fake_send(self_wxid: str, event: asyncio.Event) -> None:
        started.set()
        await event.wait()

    with (
        patch.object(worker.node_crud, "list_wxids", new=AsyncMock(return_value=["wx_a"])),
        patch.object(worker, "_send_loop", new=fake_send),
    ):
        task = asyncio.create_task(worker._supervise_send_loops(stop, running))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert "wx_a" in running
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        await asyncio.wait_for(running["wx_a"], timeout=1)


async def test_supervise_does_not_restart_running_wxid(monkeypatch) -> None:
    monkeypatch.setattr(worker.settings, "WORKER_POLL_SECONDS", 0.01)
    stop = asyncio.Event()

    async def keep_running(event: asyncio.Event) -> None:
        await event.wait()

    existing = asyncio.create_task(keep_running(stop), name="send:wx_a")
    running = {"wx_a": existing}

    with patch.object(worker.node_crud, "list_wxids", new=AsyncMock(return_value=["wx_a"])):
        task = asyncio.create_task(worker._supervise_send_loops(stop, running))
        await asyncio.sleep(0.04)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    assert running == {"wx_a": existing}
    await existing


async def test_supervise_drops_finished_task(monkeypatch) -> None:
    monkeypatch.setattr(worker.settings, "WORKER_POLL_SECONDS", 0.01)
    stop = asyncio.Event()

    async def already_done() -> None:
        return None

    finished = asyncio.create_task(already_done())
    await finished
    running = {"wx_dead": finished}

    with patch.object(worker.node_crud, "list_wxids", new=AsyncMock(return_value=[])):
        task = asyncio.create_task(worker._supervise_send_loops(stop, running))
        for _ in range(30):
            if "wx_dead" not in running:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    assert "wx_dead" not in running


async def test_supervise_crash_does_not_raise(monkeypatch) -> None:
    monkeypatch.setattr(worker.settings, "WORKER_POLL_SECONDS", 0.01)
    stop = asyncio.Event()
    running: dict[str, asyncio.Task] = {}
    with patch.object(
        worker.node_crud, "list_wxids", new=AsyncMock(side_effect=RuntimeError("db"))
    ):
        task = asyncio.create_task(worker._supervise_send_loops(stop, running))
        await asyncio.sleep(0.03)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    assert running == {}


async def test_generate_loop_runs_two_sessions_concurrently(monkeypatch) -> None:
    monkeypatch.setattr(worker.settings, "WORKER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker.settings, "INBOX_CONCURRENCY_GLOBAL", 2)
    stop = asyncio.Event()
    started = {("wx_a", "f1"): asyncio.Event(), ("wx_b", "f2"): asyncio.Event()}
    queue = [("wx_a", "f1"), ("wx_b", "f2")]

    async def fake_pick(*, exclude_sessions=None, exclude_wxids=None):
        skipped = set(exclude_sessions or ())
        for session in queue:
            if session not in skipped:
                return session
        return None

    async def fake_process(_worker_id: str, self_wxid: str, friend_wxid: str) -> bool:
        started[(self_wxid, friend_wxid)].set()
        await stop.wait()
        return True

    with (
        patch.object(worker.reply_service, "pick_next_session", new=fake_pick),
        patch.object(worker.reply_service, "process_session", new=fake_process),
    ):
        task = asyncio.create_task(worker._generate_loop(stop))
        await asyncio.wait_for(started[("wx_a", "f1")].wait(), timeout=1)
        await asyncio.wait_for(started[("wx_b", "f2")].wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)


async def test_generate_loop_does_not_double_same_session(monkeypatch) -> None:
    monkeypatch.setattr(worker.settings, "WORKER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker.settings, "INBOX_CONCURRENCY_GLOBAL", 2)
    stop = asyncio.Event()
    calls: list[tuple[str, str]] = []
    started = asyncio.Event()

    async def fake_pick(*, exclude_sessions=None, exclude_wxids=None):
        skipped = set(exclude_sessions or ())
        if ("wx_a", "f1") in skipped:
            return None
        return ("wx_a", "f1")

    async def fake_process(_worker_id: str, self_wxid: str, friend_wxid: str) -> bool:
        calls.append((self_wxid, friend_wxid))
        started.set()
        await stop.wait()
        return True

    with (
        patch.object(worker.reply_service, "pick_next_session", new=fake_pick),
        patch.object(worker.reply_service, "process_session", new=fake_process),
    ):
        task = asyncio.create_task(worker._generate_loop(stop))
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert calls == [("wx_a", "f1")]
        stop.set()
        await asyncio.wait_for(task, timeout=1)


async def test_generate_loop_drains_in_flight_on_stop(monkeypatch) -> None:
    monkeypatch.setattr(worker.settings, "WORKER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker.settings, "INBOX_CONCURRENCY_GLOBAL", 1)
    stop = asyncio.Event()
    started = asyncio.Event()
    finished = asyncio.Event()
    queue = [("wx_a", "f1")]

    async def fake_pick(*, exclude_sessions=None, exclude_wxids=None):
        if not queue:
            return None
        return queue.pop(0)

    async def fake_process(_worker_id: str, self_wxid: str, friend_wxid: str) -> bool:
        started.set()
        await asyncio.sleep(0.05)
        finished.set()
        return True

    with (
        patch.object(worker.reply_service, "pick_next_session", new=fake_pick),
        patch.object(worker.reply_service, "process_session", new=fake_process),
    ):
        task = asyncio.create_task(worker._generate_loop(stop))
        await asyncio.wait_for(started.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    assert finished.is_set()


# --- 下载循环 ---
# 循环用 asyncio.wait_for(stop.wait(), timeout=X) 等待，故用真实短超时 + 计数上界
# 来判断「是否真的在等」，而不是 monkeypatch sleep（那会绕过被测代码）。
# 判据：若不等，calls 会在观察窗内爆炸式增长；若在等，calls 受窗口/间隔约束。


async def test_download_loop_waits_between_fetches(monkeypatch) -> None:
    """账号内严格串行：每取完一条都要等一个 3~10s 区间的间隔。"""
    stop = asyncio.Event()
    calls = 0

    async def fake_process_one(_self_wxid: str) -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(worker.media_service, "interval_seconds", lambda: 0.05)
    with patch.object(worker.media_service, "process_one", new=fake_process_one):
        task = asyncio.create_task(worker._download_loop("wx_a", stop))
        await asyncio.sleep(0.22)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    # 0.22s / 0.05s ≈ 4 次；若没等，会是成百上千次
    assert 1 <= calls <= 8, f"calls={calls} 说明没有按间隔等待"


async def test_download_loop_idles_without_pending(monkeypatch) -> None:
    """没有待取媒体时按 WORKER_POLL_SECONDS 轮询，不空转。"""
    monkeypatch.setattr(worker.settings, "WORKER_POLL_SECONDS", 0.05)
    stop = asyncio.Event()
    calls = 0

    async def fake_process_one(_self_wxid: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    with patch.object(worker.media_service, "process_one", new=fake_process_one):
        task = asyncio.create_task(worker._download_loop("wx_a", stop))
        await asyncio.sleep(0.22)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    assert 1 <= calls <= 8, f"calls={calls} 说明空转没等轮询间隔"


async def test_download_loop_survives_crash(monkeypatch) -> None:
    """单次取用抛异常不能让循环退出，否则该账号媒体永久卡住、会话被永久 defer。"""
    monkeypatch.setattr(worker.settings, "WORKER_POLL_SECONDS", 0.05)
    stop = asyncio.Event()
    calls = 0

    async def boom(_self_wxid: str) -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("download crashed")

    with patch.object(worker.media_service, "process_one", new=boom):
        task = asyncio.create_task(worker._download_loop("wx_a", stop))
        await asyncio.sleep(0.22)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    assert calls >= 2, "崩溃后循环退出了"
    assert not task.cancelled()


async def test_reclaim_loop_also_fails_stale_media(monkeypatch) -> None:
    """回收循环要顺带把超时媒体翻 failed，否则会话会被一直 defer。"""
    monkeypatch.setattr(worker.settings, "RECLAIM_INTERVAL_SECONDS", 0.01)
    stop = asyncio.Event()
    seen: list[tuple[float, float]] = []

    async def fake_fail_stale(attempt_timeout_s: float, queue_timeout_s: float) -> int:
        seen.append((attempt_timeout_s, queue_timeout_s))
        stop.set()
        return 1

    with (
        patch.object(worker.inbox_crud, "reclaim_stale", new=AsyncMock(return_value=0)),
        patch.object(worker.outbox_crud, "reclaim_stale", new=AsyncMock(return_value=0)),
        patch.object(worker.media_crud, "fail_stale", new=fake_fail_stale),
    ):
        await asyncio.wait_for(asyncio.create_task(worker._reclaim_loop(stop)), timeout=2)

    # 两个阈值：「已尝试未终态」（卡死）与「从未尝试」（排队）分别判定
    assert seen == [
        (
            worker.media_service.MEDIA_ATTEMPT_TIMEOUT_SECONDS,
            worker.media_service.MEDIA_QUEUE_TIMEOUT_SECONDS,
        )
    ]
