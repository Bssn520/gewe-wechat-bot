from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, Request, Response
from pydantic import ValidationError

from app.core.config import settings
from app.core.exceptions import Unauthorized
from app.schemas.webhook import GeWeCallback
from app.services import webhook as webhook_service

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["gewe"])

MAX_BODY_BYTES = 1 << 20  # 1 MiB


@router.post("/gewe/callback")
async def gewe_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Response:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise Unauthorized(message="未授权")

    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        raw = None

    # 控制包不带凭据，只回空 200；命中即 return，不进下游。
    if isinstance(raw, dict) and (raw.get("testMsg") or raw.get("callBackUrl")):
        logger.info("webhook.ignored", reason="control_packet")
        return Response(content="", status_code=200)

    secret = settings.WEBHOOK_SECRET.strip()
    token = (authorization or "").strip().removeprefix("Bearer ")
    if not secret or token != secret:
        raise Unauthorized(message="未授权")

    if not body.strip():
        logger.info("webhook.ignored", reason="empty_body")
        return Response(content="", status_code=200)
    if raw is None:
        logger.warning("webhook.ignored", reason="invalid_json")
        return Response(content="", status_code=200)

    try:
        payload = GeWeCallback.model_validate(raw)
    except ValidationError:
        logger.warning("webhook.ignored", reason="invalid_payload")
        return Response(content="", status_code=200)

    background_tasks.add_task(webhook_service.handle_callback_safe, payload)
    return Response(content="", status_code=200)
