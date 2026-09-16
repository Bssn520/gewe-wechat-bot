"""GeWe HTTP 薄客户端。无状态，不掺业务规则。失败交给 gewe_errors 翻译。"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.core.config import settings
from app.core.gewe_errors import GeWeResult, classify_gewe_envelope, truncate_error_message

logger = structlog.get_logger(__name__)


def _new_msg_id_from_data(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    raw = data.get("newMsgId")
    if raw is None:
        return None
    return str(raw)


def _file_url_from_data(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    raw = data.get("fileUrl")
    return raw if isinstance(raw, str) and raw else None


class GeWeClient:
    def __init__(self) -> None:
        self._base = settings.GEWE_BASE_URL.rstrip("/")
        self._token = settings.GEWE_TOKEN
        self._timeout = settings.GEWE_HTTP_TIMEOUT
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        """复用长连接，避免每请求重建连接池带来的 accept/端口排队与假超时。"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def post_text(self, *, app_id: str, to_wxid: str, content: str) -> GeWeResult:
        url = f"{self._base}/gewe/v2/api/message/postText"
        headers = {
            "X-GEWE-TOKEN": self._token,
            "Content-Type": "application/json",
        }
        payload = {"appId": app_id, "toWxid": to_wxid, "content": content}
        try:
            resp = await self._http().post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            logger.warning("gewe.post_text.timeout", app_id=app_id, error=str(exc))
            return classify_gewe_envelope(http_status=None, ret=None, msg=str(exc), timed_out=True)
        except httpx.HTTPError as exc:
            logger.warning("gewe.post_text.transport", app_id=app_id, error=str(exc))
            return classify_gewe_envelope(
                http_status=None, ret=None, msg=str(exc), transport_error=True
            )

        ret: int | None = None
        msg: str | None = None
        new_msg_id: str | None = None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            raw_ret = body.get("ret")
            ret = int(raw_ret) if isinstance(raw_ret, int) else None
            msg = body.get("msg") if isinstance(body.get("msg"), str) else None
            new_msg_id = _new_msg_id_from_data(body.get("data"))
        else:
            msg = truncate_error_message(resp.text)

        result = classify_gewe_envelope(
            http_status=resp.status_code,
            ret=ret,
            msg=msg,
            new_msg_id=new_msg_id,
        )
        if not result.ok:
            logger.warning(
                "gewe.post_text.failed",
                app_id=app_id,
                error_code=result.error_code,
                ret=result.ret,
            )
        return result

    async def download_image(self, *, app_id: str, xml: str, type: int) -> GeWeResult:
        """下载图片：返回 data.fileUrl（**签名 URL，非文件字节**，约 7 天有效）。

        `xml` 传**回调 content 原文**（完整 `<msg>...</msg>`），不是只传 `<img/>` 片段。
        `type`：1 高清 / 2 常规 / 3 缩略图；不是所有图都有高清与常规版本。
        """
        url = f"{self._base}/gewe/v2/api/message/downloadImage"
        headers = {
            "X-GEWE-TOKEN": self._token,
            "Content-Type": "application/json",
        }
        payload = {"appId": app_id, "xml": xml, "type": type}
        try:
            resp = await self._http().post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            logger.warning("gewe.download_image.timeout", app_id=app_id, type=type, error=str(exc))
            return classify_gewe_envelope(http_status=None, ret=None, msg=str(exc), timed_out=True)
        except httpx.HTTPError as exc:
            logger.warning(
                "gewe.download_image.transport", app_id=app_id, type=type, error=str(exc)
            )
            return classify_gewe_envelope(
                http_status=None, ret=None, msg=str(exc), transport_error=True
            )

        ret: int | None = None
        msg: str | None = None
        file_url: str | None = None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            raw_ret = body.get("ret")
            ret = int(raw_ret) if isinstance(raw_ret, int) else None
            msg = body.get("msg") if isinstance(body.get("msg"), str) else None
            file_url = _file_url_from_data(body.get("data"))
        else:
            msg = truncate_error_message(resp.text)

        result = classify_gewe_envelope(
            http_status=resp.status_code,
            ret=ret,
            msg=msg,
            file_url=file_url,
        )
        if not result.ok:
            logger.warning(
                "gewe.download_image.failed",
                app_id=app_id,
                type=type,
                error_code=result.error_code,
                ret=result.ret,
            )
        return result


gewe_client = GeWeClient()
