"""HTTP 中间件：纯 ASGI，不使用 BaseHTTPMiddleware（避免 anyio cancel scope）。"""

from __future__ import annotations

import asyncio
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

logger = structlog.get_logger(__name__)

_MAX_REQUEST_ID_LEN = 128


def _is_health_probe(method: str, path: str) -> bool:
    """Docker / 编排器的存活探测，不记业务请求日志。"""
    return method == "GET" and path == f"{settings.API_V1_PREFIX}/health"


def _request_id_from_scope(scope: Scope) -> str:
    raw = ""
    for key, value in scope.get("headers") or []:
        if key.lower() == b"x-request-id":
            raw = value.decode("latin-1", errors="replace")
            break
    cleaned = "".join(ch for ch in raw if ch.isprintable() and ch not in "\r\n").strip()
    if not cleaned:
        return str(uuid.uuid4())
    if len(cleaned) > _MAX_REQUEST_ID_LEN:
        return cleaned[:_MAX_REQUEST_ID_LEN]
    return cleaned


class LoggingMiddleware:
    """生成 Request-ID，并在响应 body 真正结束（或失败/断开）时记日志。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from_scope(scope)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        skip_request_log = _is_health_probe(method, path)
        client = scope.get("client")
        client_ip = client[0] if client else None
        start_time = time.perf_counter()
        if not skip_request_log:
            logger.info(
                "http.request.start",
                method=method,
                path=path,
                client_ip=client_ip,
            )

        status_code: int | None = None
        ended = False

        def _duration_ms() -> float:
            return round((time.perf_counter() - start_time) * 1000, 2)

        def _mark_ended() -> bool:
            nonlocal ended
            if ended:
                return False
            ended = True
            return True

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            outgoing = message
            if message["type"] == "http.response.start":
                raw_status = message.get("status")
                if raw_status is not None:
                    status_code = int(raw_status)
                headers = MutableHeaders(raw=list(message.get("headers", [])))
                headers["X-Request-ID"] = request_id
                outgoing = {**message, "headers": headers.raw}
            await send(outgoing)
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
                and _mark_ended()
                and not skip_request_log
            ):
                logger.info(
                    "http.request.end",
                    method=method,
                    path=path,
                    status_code=status_code,
                    duration_ms=_duration_ms(),
                )

        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            if _mark_ended() and not skip_request_log:
                logger.info(
                    "http.request.disconnected",
                    method=method,
                    path=path,
                    status_code=status_code,
                    duration_ms=_duration_ms(),
                )
            raise
        except Exception as exc:
            if _mark_ended() and not skip_request_log:
                logger.exception(
                    "http.request.failed",
                    method=method,
                    path=path,
                    status_code=status_code,
                    duration_ms=_duration_ms(),
                    error=str(exc),
                )
            raise
        finally:
            if _mark_ended() and not skip_request_log:
                logger.info(
                    "http.request.end",
                    method=method,
                    path=path,
                    status_code=status_code,
                    duration_ms=_duration_ms(),
                )
