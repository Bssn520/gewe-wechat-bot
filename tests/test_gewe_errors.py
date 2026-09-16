from __future__ import annotations

import pytest

from app.core.gewe_errors import (
    ERROR_MESSAGE_MAX,
    GEWE_AUTH,
    GEWE_BAD_REQUEST,
    GEWE_OFFLINE,
    GEWE_RISK,
    GEWE_TIMEOUT,
    GEWE_UNAVAILABLE,
    GEWE_UNKNOWN,
    classify_gewe_envelope,
    truncate_error_message,
)


def test_ret_200_success() -> None:
    r = classify_gewe_envelope(http_status=200, ret=200, msg="操作成功", new_msg_id="123")
    assert r.ok
    assert r.error_code is None
    assert r.new_msg_id == "123"


def test_token_missing_is_auth() -> None:
    r = classify_gewe_envelope(
        http_status=200,
        ret=500,
        msg="header:X-GEWE-TOKEN 不可为空",
    )
    assert not r.ok
    assert r.error_code == GEWE_AUTH


def test_unknown_ret() -> None:
    r = classify_gewe_envelope(http_status=200, ret=418, msg="奇怪错误")
    assert not r.ok
    assert r.error_code == GEWE_UNKNOWN


def test_timeout_and_transport_take_precedence() -> None:
    timeout = classify_gewe_envelope(http_status=200, ret=200, msg="ok", timed_out=True)
    assert timeout.error_code == GEWE_TIMEOUT
    transport = classify_gewe_envelope(http_status=200, ret=200, msg="boom", transport_error=True)
    assert transport.error_code == GEWE_UNAVAILABLE


@pytest.mark.parametrize(
    ("msg", "code"),
    [
        ("账号已离线", GEWE_OFFLINE),
        ("触发风控", GEWE_RISK),
        ("参数错误", GEWE_BAD_REQUEST),
    ],
)
def test_msg_hints(msg: str, code: str) -> None:
    r = classify_gewe_envelope(http_status=200, ret=500, msg=msg)
    assert r.error_code == code


def test_http_5xx_without_token_is_unavailable() -> None:
    r = classify_gewe_envelope(http_status=503, ret=None, msg="gateway")
    assert r.error_code == GEWE_UNAVAILABLE


def test_ret_400_is_bad_request() -> None:
    r = classify_gewe_envelope(http_status=200, ret=400, msg="bad")
    assert r.error_code == GEWE_BAD_REQUEST


def test_truncate_error_message() -> None:
    assert truncate_error_message("  hi  ") == "hi"
    long = "x" * (ERROR_MESSAGE_MAX + 10)
    assert len(truncate_error_message(long)) == ERROR_MESSAGE_MAX
