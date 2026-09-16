"""上游信封 → 内部 error_code。Service 只看集合，禁止比对中文。

GeWe 官方几乎没有失败码表。映射以 ret 为主、msg 只作辅助分类。
未识别 → gewe_unknown（发送侧默认不重试）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

ERROR_MESSAGE_MAX: Final = 500

# --- 内部码 ---
GEWE_TIMEOUT: Final = "gewe_timeout"
GEWE_UNAVAILABLE: Final = "gewe_unavailable"
GEWE_OFFLINE: Final = "gewe_offline"
GEWE_RISK: Final = "gewe_risk"
GEWE_AUTH: Final = "gewe_auth"
GEWE_BAD_REQUEST: Final = "gewe_bad_request"
GEWE_UNKNOWN: Final = "gewe_unknown"

LLM_TIMEOUT: Final = "llm_timeout"
LLM_RATE_LIMITED: Final = "llm_rate_limited"
LLM_BAD_REQUEST: Final = "llm_bad_request"
LLM_UNAVAILABLE: Final = "llm_unavailable"
LLM_EMPTY: Final = "llm_empty"
LLM_UNKNOWN: Final = "llm_unknown"

SEND_RETRY_ONCE: Final[frozenset[str]] = frozenset({GEWE_TIMEOUT, GEWE_UNAVAILABLE})
SEND_CIRCUIT_NODE: Final[frozenset[str]] = frozenset({GEWE_OFFLINE, GEWE_RISK})
SEND_CIRCUIT_GLOBAL: Final[frozenset[str]] = frozenset({GEWE_AUTH})
INBOX_NO_RETRY: Final[frozenset[str]] = frozenset(
    {
        LLM_TIMEOUT,
        LLM_RATE_LIMITED,
        LLM_BAD_REQUEST,
        LLM_UNAVAILABLE,
        LLM_EMPTY,
        LLM_UNKNOWN,
    }
)

_OFFLINE_HINTS: Final = ("离线", "未登录", "登录过期")
_RISK_HINTS: Final = ("风控", "限制", "频", "封")
_AUTH_HINTS: Final = ("x-gewe-token", "token")
_BAD_REQ_HINTS: Final = ("参数", "不可为空")


@dataclass(frozen=True, slots=True)
class GeWeResult:
    """gewe_client 返回给 Service 的翻译结果。"""

    ok: bool
    error_code: str | None
    error_message: str
    new_msg_id: str | None = None
    file_url: str | None = None
    ret: int | None = None


def truncate_error_message(text: str | None) -> str:
    raw = (text or "").strip()
    if len(raw) <= ERROR_MESSAGE_MAX:
        return raw
    return raw[:ERROR_MESSAGE_MAX]


def classify_gewe_envelope(
    *,
    http_status: int | None,
    ret: int | None,
    msg: str | None,
    new_msg_id: str | None = None,
    file_url: str | None = None,
    timed_out: bool = False,
    transport_error: bool = False,
) -> GeWeResult:
    """把 HTTP + {ret, msg, data} 译成内部码。"""
    message = truncate_error_message(msg)

    if timed_out:
        return GeWeResult(ok=False, error_code=GEWE_TIMEOUT, error_message=message or "timeout")
    if transport_error:
        return GeWeResult(
            ok=False, error_code=GEWE_UNAVAILABLE, error_message=message or "transport_error"
        )

    if http_status == 200 and ret == 200:
        return GeWeResult(
            ok=True,
            error_code=None,
            error_message="",
            new_msg_id=new_msg_id,
            file_url=file_url,
            ret=ret,
        )

    lowered = (msg or "").lower()
    if ret == 500 and "x-gewe-token" in lowered:
        return GeWeResult(ok=False, error_code=GEWE_AUTH, error_message=message, ret=ret)
    if ret in {401, 403} or any(h in lowered for h in _AUTH_HINTS):
        return GeWeResult(ok=False, error_code=GEWE_AUTH, error_message=message, ret=ret)

    if msg and any(h in msg for h in _OFFLINE_HINTS):
        return GeWeResult(ok=False, error_code=GEWE_OFFLINE, error_message=message, ret=ret)
    if msg and any(h in msg for h in _RISK_HINTS):
        return GeWeResult(ok=False, error_code=GEWE_RISK, error_message=message, ret=ret)

    if ret == 400 or (msg and any(h in msg for h in _BAD_REQ_HINTS) and "token" not in lowered):
        return GeWeResult(ok=False, error_code=GEWE_BAD_REQUEST, error_message=message, ret=ret)

    if (http_status is not None and http_status >= 500) or ret in {500, 502, 503}:
        return GeWeResult(ok=False, error_code=GEWE_UNAVAILABLE, error_message=message, ret=ret)

    return GeWeResult(ok=False, error_code=GEWE_UNKNOWN, error_message=message, ret=ret)
