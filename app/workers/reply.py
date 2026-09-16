"""独立 worker：生成循环 + 每 self_wxid 一个发送循环 + 租约回收。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import uuid

import structlog
from tortoise import Tortoise

from app.agno import validate_agent_config
from app.core.config import TORTOISE_ORM, settings
from app.core.locks import worker_single_instance_lock
from app.core.logging import configure_logging
from app.crud import inbox as inbox_crud
from app.crud import media as media_crud
from app.crud import node as node_crud
from app.crud import outbox as outbox_crud
from app.services import media as media_service
from app.services import outbound as outbound_service
from app.services import reply as reply_service
from app.utils.gewe_client import gewe_client

logger = structlog.get_logger(__name__)

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def missing_send_loops(known: list[str], running: set[str]) -> list[str]:
    """监督循环要新拉起的号。"""
    return [wxid for wxid in known if wxid not in running]


async def _generate_loop(stop: asyncio.Event) -> None:
    """调度器占槽：最多 GLOBAL 路不同会话同时 process_session。"""
    sem = asyncio.Semaphore(settings.INBOX_CONCURRENCY_GLOBAL)
    in_flight: set[tuple[str, str]] = set()
    tasks: set[asyncio.Task] = set()

    async def _run_session(self_wxid: str, friend_wxid: str) -> None:
        try:
            await reply_service.process_session(WORKER_ID, self_wxid, friend_wxid)
        except Exception:
            logger.exception("worker.generate.crash", self_wxid=self_wxid, friend_wxid=friend_wxid)
        finally:
            in_flight.discard((self_wxid, friend_wxid))
            sem.release()

    while not stop.is_set():
        try:
            await asyncio.wait_for(sem.acquire(), timeout=settings.WORKER_POLL_SECONDS)
        except TimeoutError:
            continue
        if stop.is_set():
            sem.release()
            break
        try:
            peeked = await reply_service.pick_next_session(exclude_sessions=in_flight)
        except Exception:
            logger.exception("worker.generate.crash")
            sem.release()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.WORKER_POLL_SECONDS)
            continue
        if peeked is None:
            sem.release()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.WORKER_POLL_SECONDS)
            continue
        self_wxid, friend_wxid = peeked
        in_flight.add((self_wxid, friend_wxid))
        task = asyncio.create_task(
            _run_session(self_wxid, friend_wxid),
            name=f"gen:{self_wxid}:{friend_wxid}",
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _send_loop(self_wxid: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            worked = await outbound_service.process_one(self_wxid)
        except Exception:
            logger.exception("worker.send.crash", self_wxid=self_wxid)
            worked = False
        if not worked:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.WORKER_POLL_SECONDS)


async def _reclaim_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            n_in = await inbox_crud.reclaim_stale()
            n_out = await outbox_crud.reclaim_stale()
            # 媒体 pending 超时翻 failed：同时解除生成侧对该会话的 defer
            n_media = await media_crud.fail_stale(
                media_service.MEDIA_ATTEMPT_TIMEOUT_SECONDS,
                media_service.MEDIA_QUEUE_TIMEOUT_SECONDS,
            )
            if n_in or n_out or n_media:
                logger.warning("worker.reclaimed", inbox=n_in, outbox=n_out, media=n_media)
        except Exception:
            logger.exception("worker.reclaim.crash")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.RECLAIM_INTERVAL_SECONDS)


async def _download_loop(self_wxid: str, stop: asyncio.Event) -> None:
    """账号内**严格串行**取媒体，账号间并行。

    下载消耗账号会话（GeWe 用你的账号去微信 CDN 取文件），官方要求串行 +
    每条 3~10 秒，频率高会掉线——所以这里每条之间必须等，且不能复用发送的令牌桶。
    """
    while not stop.is_set():
        try:
            worked = await media_service.process_one(self_wxid)
        except Exception:
            logger.exception("worker.download.crash", self_wxid=self_wxid)
            worked = False
        if not worked:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.WORKER_POLL_SECONDS)
            continue
        # 取过一条就等：下一轮拿 pending 时可能还有活，但必须间隔够
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=media_service.interval_seconds())


async def _supervise_send_loops(stop: asyncio.Event, running: dict[str, asyncio.Task]) -> None:
    while not stop.is_set():
        try:
            known = await node_crud.list_wxids()
            for wxid in missing_send_loops(known, set(running)):
                running[wxid] = asyncio.create_task(_send_loop(wxid, stop), name=f"send:{wxid}")
                logger.info("worker.send_loop.started", self_wxid=wxid)
            for wxid, task in list(running.items()):
                if task.done():
                    running.pop(wxid, None)
        except Exception:
            logger.exception("worker.supervise.crash")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.WORKER_POLL_SECONDS)


async def _supervise_download_loops(stop: asyncio.Event, running: dict[str, asyncio.Task]) -> None:
    while not stop.is_set():
        try:
            known = await node_crud.list_wxids()
            for wxid in missing_send_loops(known, set(running)):
                running[wxid] = asyncio.create_task(
                    _download_loop(wxid, stop), name=f"download:{wxid}"
                )
                logger.info("worker.download_loop.started", self_wxid=wxid)
            for wxid, task in list(running.items()):
                if task.done():
                    running.pop(wxid, None)
        except Exception:
            logger.exception("worker.supervise_download.crash")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.WORKER_POLL_SECONDS)


async def _run() -> None:
    # fail fast：NODE_AGENTS 引用了未注册的 Agent 就地崩溃，避免静默回落成默认人设
    validate_agent_config()
    await Tortoise.init(config=TORTOISE_ORM)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with worker_single_instance_lock() as ok:
        if not ok:
            logger.error("worker.exit.lock_held")
            await Tortoise.close_connections()
            raise SystemExit(1)
        logger.info("worker.started", worker_id=WORKER_ID)
        senders: dict[str, asyncio.Task] = {}
        downloaders: dict[str, asyncio.Task] = {}
        tasks = [
            asyncio.create_task(_generate_loop(stop), name="generate"),
            asyncio.create_task(_reclaim_loop(stop), name="reclaim"),
            asyncio.create_task(_supervise_send_loops(stop, senders), name="supervise"),
            asyncio.create_task(
                _supervise_download_loops(stop, downloaders), name="supervise_download"
            ),
        ]
        await stop.wait()
        logger.info("worker.draining")
        await asyncio.gather(
            *tasks, *senders.values(), *downloaders.values(), return_exceptions=True
        )
    await gewe_client.aclose()
    await Tortoise.close_connections()
    logger.info("worker.stopped")


def main() -> None:
    configure_logging(json_log=settings.is_prod, log_level="DEBUG" if settings.DEBUG else "INFO")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
