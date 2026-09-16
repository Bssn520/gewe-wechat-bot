"""模型工厂共享逻辑：按渠道从 Settings 组装客户端参数。"""

from __future__ import annotations

from app.agno.models.channels.base import ChannelSpec
from app.core.config import settings


def _setting_str(field_name: str) -> str:
    """读取 Settings 上的字符串配置；字段不存在或空则返回 ''。"""
    if not field_name:
        return ""
    value = getattr(settings, field_name, "") or ""
    return value if isinstance(value, str) else str(value)


def client_kwargs_for_channel(channel: ChannelSpec) -> dict[str, str]:
    """从 Settings（env）加载 base_url / api_key，供 OpenAI 兼容客户端使用。"""
    kwargs: dict[str, str] = {}
    base_url = _setting_str(channel.base_url_setting)
    if base_url:
        kwargs["base_url"] = base_url
    api_key = _setting_str(channel.api_key_setting)
    if api_key:
        kwargs["api_key"] = api_key
    return kwargs
