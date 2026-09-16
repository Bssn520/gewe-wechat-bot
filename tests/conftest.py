from __future__ import annotations

import os

os.environ["APP_ENV"] = "test"
os.environ["GEWE_ALLOWLIST"] = "wxid_self:wxid_friend1"
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("GEWE_TOKEN", "test-token")
os.environ["DB_NAME"] = "gewe_test"

import pytest
from fastapi.testclient import TestClient
from tortoise import Tortoise

from app.core.config import TORTOISE_ORM, settings
from app.main import app  # noqa: E402

TEXT_PAYLOAD = {
    "appid": "wx_test_app",
    "wxid": "wxid_self",
    "content": "你好",
    "fromUser": "wxid_friend1",
    "toUser": "wxid_self",
    "isSelf": False,
    "msgId": 1,
    "newMsgId": 91540001,
    "msgType": "TEXT",
}

# GeWe 回调把密钥原样放在 Authorization 头（无 Bearer 前缀），见 app/api/v1/webhook.py
GEWE_HEADERS = {"Authorization": "test-secret"}

_TRUNCATE_SQL = "TRUNCATE TABLE media, inbox, outbox, chat_sessions, nodes RESTART IDENTITY CASCADE"


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def text_payload() -> dict:
    return dict(TEXT_PAYLOAD)


@pytest.fixture
async def db():
    """连独立 gewe_test。拒绝误连 dev/prod。"""
    if not settings.is_test or settings.DB_NAME != "gewe_test":
        raise RuntimeError(
            f"拒绝在非测试库跑 DB 用例: APP_ENV={settings.APP_ENV} DB_NAME={settings.DB_NAME}"
        )
    await Tortoise.init(config=TORTOISE_ORM)
    conn = Tortoise.get_connection("default")
    await conn.execute_query(_TRUNCATE_SQL)
    try:
        yield
    finally:
        await conn.execute_query(_TRUNCATE_SQL)
        await Tortoise.close_connections()
