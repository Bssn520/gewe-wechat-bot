from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.crud.node import UpsertOutcome
from tests.conftest import GEWE_HEADERS, TEXT_PAYLOAD


def _post(client: TestClient, payload: dict, headers: dict | None = None):
    return client.post("/gewe/callback", json=payload, headers=headers or GEWE_HEADERS)


def _upsert_ok() -> AsyncMock:
    return AsyncMock(return_value=UpsertOutcome.CREATED)


def test_webhook_empty_200_and_inserts(client: TestClient) -> None:
    from app.models.inbox import Inbox

    fake = Inbox(
        id=1,
        self_wxid="wxid_self",
        app_id="wx_test_app",
        friend_wxid="wxid_friend1",
        new_msg_id="91540001",
    )
    insert = AsyncMock(return_value=fake)
    with (
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()),
        patch("app.services.webhook.inbox_crud.insert_pending", new=insert) as ins,
    ):
        resp = _post(client, TEXT_PAYLOAD)
    assert resp.status_code == 200
    assert resp.content == b""
    ins.assert_awaited_once()


def test_webhook_duplicate_still_200(client: TestClient) -> None:
    with (
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()),
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock(return_value=None)),
    ):
        resp = _post(client, TEXT_PAYLOAD)
    assert resp.status_code == 200
    assert resp.content == b""


@pytest.mark.parametrize(
    "override",
    [
        {"toUser": "123@chatroom"},
        {"fromUser": "123@chatroom"},
        {"content": "wxid_friend1:\n大家好"},  # v2 群：顶层无 @chatroom，正文带发言人前缀
        {"content": "验证回调地址是否可用"},
        {"msgType": "VOICE"},  # 图片已支持；语音/视频仍忽略
        {"fromUser": "wxid_stranger"},
        {"isSelf": True},
        {"fromUser": "gh_official"},
        {"newMsgId": None},
        {"appid": ""},
        {"wxid": ""},
    ],
)
def test_ignored_payloads_not_inserted(client: TestClient, override: dict) -> None:
    payload = {**TEXT_PAYLOAD, **override}
    with (
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()) as upsert,
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins,
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200
    assert resp.content == b""
    ins.assert_not_called()
    if override.get("fromUser") == "wxid_stranger":
        upsert.assert_awaited_once()
    else:
        upsert.assert_not_called()


def test_private_text_with_unrelated_wxid_prefix_is_inserted(client: TestClient) -> None:
    """正文碰巧以别人的 wxid: 开头，不是群前缀，仍按私聊落库。"""
    payload = {**TEXT_PAYLOAD, "content": "wxid_other:\n这是私聊"}
    with (
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()),
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins,
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200
    ins.assert_awaited_once()


def test_image_payload_inserted_with_media_source(client: TestClient) -> None:
    """图片消息要落库：content 留空（媒体不占 content），XML 进 media_source。"""
    xml = '<msg><img aeskey="k" cdnmidimgurl="f" length="100" /></msg>'
    payload = {**TEXT_PAYLOAD, "msgType": "IMAGE", "content": xml}
    with (
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()),
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins,
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200
    assert resp.content == b""
    ins.assert_awaited_once()
    kwargs = ins.await_args.kwargs
    assert kwargs["media_type"] == "image"
    assert kwargs["media_source"] == {"xml": xml}
    assert kwargs["content"] is None  # 原始 XML 不进 content


def test_unknown_node_not_upserted(client: TestClient) -> None:
    payload = {**TEXT_PAYLOAD, "wxid": "wxid_other", "toUser": "wxid_other"}
    with (
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()) as upsert,
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins,
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200
    upsert.assert_not_called()
    ins.assert_not_called()


def test_upsert_rejected_skips_inbox(client: TestClient) -> None:
    with (
        patch(
            "app.services.webhook.node_crud.upsert",
            new=AsyncMock(return_value=UpsertOutcome.REJECTED),
        ),
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins,
    ):
        resp = _post(client, TEXT_PAYLOAD)
    assert resp.status_code == 200
    ins.assert_not_called()


def test_system_online_rotates_existing_slot(client: TestClient) -> None:
    payload = {**TEXT_PAYLOAD, "msgType": "LOGIN_SUCCESS", "appid": "slot_new"}
    upsert = AsyncMock(return_value=UpsertOutcome.ROTATED)
    with (
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins,
        patch("app.services.webhook.node_crud.upsert", new=upsert),
        patch("app.services.webhook.node_crud.get_app_id", new=AsyncMock(return_value="slot_old")),
        patch("app.services.webhook.circuit_crud.open_circuit", new=AsyncMock()) as circ,
        patch("app.services.webhook.circuit_crud.close_if_expired", new=AsyncMock()) as close,
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200
    ins.assert_not_called()
    upsert.assert_awaited_once_with(self_wxid="wxid_self", app_id="slot_new")
    circ.assert_not_called()
    close.assert_awaited()


def test_empty_allowlist_inserts(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.services.webhook.settings.GEWE_ALLOWLIST", "")
    insert = AsyncMock(return_value=None)
    with (
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()) as upsert,
        patch("app.services.webhook.inbox_crud.insert_pending", new=insert) as ins,
    ):
        resp = _post(client, TEXT_PAYLOAD)
    assert resp.status_code == 200
    upsert.assert_awaited_once()
    ins.assert_awaited_once()


def test_system_offline_opens_circuit_not_inbox(client: TestClient) -> None:
    payload = {**TEXT_PAYLOAD, "msgType": "LOGOUT"}
    with (
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins,
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()) as upsert,
        patch("app.services.webhook.circuit_crud.open_circuit", new=AsyncMock()) as circ,
        patch("app.services.webhook.circuit_crud.close_if_expired", new=AsyncMock()) as close,
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200
    ins.assert_not_called()
    upsert.assert_not_called()
    circ.assert_awaited()
    close.assert_not_called()


def test_system_online_rejected_slot_still_closes_expired(client: TestClient) -> None:
    payload = {**TEXT_PAYLOAD, "msgType": "LOGIN_SUCCESS", "appid": "slot_taken"}
    upsert = AsyncMock(return_value=UpsertOutcome.REJECTED)
    with (
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins,
        patch("app.services.webhook.node_crud.upsert", new=upsert),
        patch("app.services.webhook.node_crud.get_app_id", new=AsyncMock(return_value="slot_old")),
        patch("app.services.webhook.circuit_crud.close_if_expired", new=AsyncMock()) as close,
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200
    ins.assert_not_called()
    upsert.assert_awaited_once()
    close.assert_awaited_once_with("wxid_self")


def test_system_offline_without_wxid_looks_up_node(client: TestClient) -> None:
    payload = {**TEXT_PAYLOAD, "msgType": "LOGOUT", "wxid": ""}
    with (
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins,
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()) as upsert,
        patch(
            "app.services.webhook.node_crud.get_wxid",
            new=AsyncMock(return_value="wx_a"),
        ) as lookup,
        patch(
            "app.services.webhook.circuit_crud.open_circuit",
            new=AsyncMock(return_value=True),
        ) as circ,
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200
    ins.assert_not_called()
    upsert.assert_not_called()
    lookup.assert_awaited_once_with("wx_test_app")
    circ.assert_awaited_once()
    assert circ.await_args.args[0] == "wx_a"


def test_system_offline_unknown_app_skips_circuit(client: TestClient) -> None:
    payload = {**TEXT_PAYLOAD, "msgType": "LOGOUT", "wxid": ""}
    with (
        patch("app.services.webhook.node_crud.get_wxid", new=AsyncMock(return_value=None)),
        patch("app.services.webhook.circuit_crud.open_circuit", new=AsyncMock()) as circ,
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200
    circ.assert_not_called()


def test_system_online_closes_expired_not_inbox(client: TestClient) -> None:
    payload = {**TEXT_PAYLOAD, "msgType": "LOGIN_SUCCESS"}
    with (
        patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins,
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()) as upsert,
        patch("app.services.webhook.node_crud.get_app_id", new=AsyncMock(return_value=None)),
        patch("app.services.webhook.circuit_crud.open_circuit", new=AsyncMock()) as circ,
        patch("app.services.webhook.circuit_crud.close_if_expired", new=AsyncMock()) as close,
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200
    ins.assert_not_called()
    upsert.assert_not_called()
    circ.assert_not_called()
    close.assert_awaited()


def test_node_timeout_still_inserts_inbox(client: TestClient) -> None:
    """建号瞬时超时不能丢消息：重试后仍要落 inbox。"""
    ins = AsyncMock(return_value=None)
    with (
        patch(
            "app.services.webhook.node_crud.upsert",
            new=AsyncMock(side_effect=TimeoutError),
        ) as upsert,
        patch("app.services.webhook.inbox_crud.insert_pending", new=ins),
    ):
        resp = _post(client, TEXT_PAYLOAD)
    assert resp.status_code == 200
    assert upsert.await_count == 2
    ins.assert_awaited_once()


def test_node_error_still_inserts_inbox(client: TestClient) -> None:
    """建号抛错（如旧实现的事务中止）也不能丢消息。"""
    ins = AsyncMock(return_value=None)
    with (
        patch(
            "app.services.webhook.node_crud.upsert",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ) as upsert,
        patch("app.services.webhook.inbox_crud.insert_pending", new=ins),
    ):
        resp = _post(client, TEXT_PAYLOAD)
    assert resp.status_code == 200
    assert upsert.await_count == 2
    ins.assert_awaited_once()


def test_node_retry_succeeds_on_second_attempt(client: TestClient) -> None:
    """第一次超时、第二次成功：重试生效，消息只落一次。"""
    upsert = AsyncMock(side_effect=[TimeoutError, UpsertOutcome.CREATED])
    ins = AsyncMock(return_value=None)
    with (
        patch("app.services.webhook.node_crud.upsert", new=upsert),
        patch("app.services.webhook.inbox_crud.insert_pending", new=ins),
    ):
        resp = _post(client, TEXT_PAYLOAD)
    assert resp.status_code == 200
    assert upsert.await_count == 2
    ins.assert_awaited_once()


def test_insert_timeout_still_empty_200(client: TestClient) -> None:
    with (
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()),
        patch(
            "app.services.webhook.inbox_crud.insert_pending",
            new=AsyncMock(side_effect=TimeoutError),
        ),
    ):
        resp = _post(client, TEXT_PAYLOAD)
    assert resp.status_code == 200
    assert resp.content == b""


def test_insert_error_still_empty_200(client: TestClient) -> None:
    with (
        patch("app.services.webhook.node_crud.upsert", new=_upsert_ok()),
        patch(
            "app.services.webhook.inbox_crud.insert_pending",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        resp = _post(client, TEXT_PAYLOAD)
    assert resp.status_code == 200
    assert resp.content == b""


def test_empty_body_still_empty_200(client: TestClient) -> None:
    resp = client.post("/gewe/callback", content=b"", headers=GEWE_HEADERS)
    assert resp.status_code == 200
    assert resp.content == b""


def test_invalid_json_still_empty_200(client: TestClient) -> None:
    resp = client.post(
        "/gewe/callback",
        content=b"not-json",
        headers={**GEWE_HEADERS, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.content == b""


def test_missing_authorization_is_401(client: TestClient) -> None:
    resp = client.post("/gewe/callback", content=b"")
    assert resp.status_code == 401
    assert resp.json()["code"] == 1003


def test_wrong_authorization_is_401(client: TestClient) -> None:
    resp = client.post("/gewe/callback", content=b"", headers={"Authorization": "wrong-secret"})
    assert resp.status_code == 401


def test_x_gewe_token_header_no_longer_accepted(client: TestClient) -> None:
    """入站鉴权头是 Authorization；旧的 X-GEWE-TOKEN 不再被采信（回归护栏）。"""
    resp = client.post("/gewe/callback", content=b"", headers={"X-GEWE-TOKEN": "test-secret"})
    assert resp.status_code == 401


def test_bearer_prefixed_authorization_accepted(client: TestClient) -> None:
    resp = client.post(
        "/gewe/callback", content=b"", headers={"Authorization": "Bearer test-secret"}
    )
    assert resp.status_code == 200
    assert resp.content == b""


# ── 控制包：不带 Authorization，按形状 ACK 空 200 且不落库 ──

VERIFY_PAYLOAD = {"testMsg": "回调地址链接成功！", "token": "test-token"}


def test_verify_packet_acked_without_credential(client: TestClient) -> None:
    resp = client.post("/gewe/callback", json=VERIFY_PAYLOAD)
    assert resp.status_code == 200
    assert resp.content == b""


def test_verify_packet_acked_regardless_of_body_token(client: TestClient) -> None:
    """body.token 不是凭据，验不验都当控制包放行。"""
    resp = client.post("/gewe/callback", json={**VERIFY_PAYLOAD, "token": "wrong"})
    assert resp.status_code == 200
    assert resp.content == b""


def test_verify_packet_is_not_persisted(client: TestClient) -> None:
    with patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins:
        resp = client.post("/gewe/callback", json=VERIFY_PAYLOAD)
    assert resp.status_code == 200
    assert resp.content == b""
    ins.assert_not_awaited()


def test_verify_shaped_packet_with_business_fields_is_inert(client: TestClient) -> None:
    """testMsg 是空转出口：带业务字段也绝不进下游。"""
    forged = {**TEXT_PAYLOAD, "testMsg": "回调地址链接成功！"}
    with patch("app.api.v1.webhook.webhook_service.handle_callback_safe", new=AsyncMock()) as h:
        resp = client.post("/gewe/callback", json=forged)
    assert resp.status_code == 200
    assert resp.content == b""
    h.assert_not_awaited()


def test_oversized_body_rejected(client: TestClient) -> None:
    from app.api.v1.webhook import MAX_BODY_BYTES

    resp = client.post(
        "/gewe/callback",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers=GEWE_HEADERS,
    )
    assert resp.status_code == 401


# ── 订阅确认包：不带 Authorization，需放行（否则控制台「一键检测」永远失败）──

SUBSCRIBE_CONFIRM = {
    "msg": "设置订阅成功!!",
    "callBackUrl": "https://example.cpolar.top/gewe/callback",
}


def test_subscription_confirm_acked_without_auth(client: TestClient) -> None:
    resp = client.post("/gewe/callback", json=SUBSCRIBE_CONFIRM)
    assert resp.status_code == 200
    assert resp.content == b""


def test_subscription_confirm_not_persisted(client: TestClient) -> None:
    with patch("app.services.webhook.inbox_crud.insert_pending", new=AsyncMock()) as ins:
        resp = client.post("/gewe/callback", json=SUBSCRIBE_CONFIRM)
    assert resp.status_code == 200
    ins.assert_not_awaited()


def test_subscription_confirm_shape_does_not_open_message_injection(client: TestClient) -> None:
    """带业务字段也仍走空转出口：无凭据一律不进下游。"""
    forged = {**TEXT_PAYLOAD, "msg": "设置订阅成功!!", "callBackUrl": "https://x/y"}
    with patch("app.api.v1.webhook.webhook_service.handle_callback_safe", new=AsyncMock()) as h:
        resp = client.post("/gewe/callback", json=forged)
    assert resp.status_code == 200
    assert resp.content == b""
    h.assert_not_awaited()
