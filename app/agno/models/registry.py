"""从文件注册表解析渠道与模型 id。"""

from __future__ import annotations

from app.agno.models.channels import CHANNELS, DEFAULTS, ChannelSpec
from app.agno.models.types import ModelKind


def get_channel(channel_id: str) -> ChannelSpec:
    """按 id 取渠道；不存在则 ValueError。"""
    try:
        return CHANNELS[channel_id]
    except KeyError as exc:
        known = ", ".join(sorted(CHANNELS)) or "(无)"
        raise ValueError(f"未知模型渠道: {channel_id!r}；已注册: {known}") from exc


def resolve_model(
    kind: ModelKind,
    *,
    channel_id: str | None = None,
    model_id: str | None = None,
    validate_catalog: bool = True,
) -> tuple[ChannelSpec, str]:
    """解析某能力的渠道 + model_id。未传时使用 `channels.DEFAULTS`。"""
    if channel_id is None or model_id is None:
        if kind not in DEFAULTS:
            configured = ", ".join(sorted(DEFAULTS)) or "(无)"
            channels = ", ".join(sorted(CHANNELS)) or "(无)"
            raise ValueError(
                f"能力 {kind!r} 尚未配置 DEFAULTS，且未同时指定 channel_id 与 model_id；"
                f"已配置默认: {configured}；已注册渠道: {channels}"
            )
        default_channel_id, default_model_id = DEFAULTS[kind]
        resolved_channel_id = channel_id or default_channel_id
        resolved_model_id = model_id or default_model_id
    else:
        resolved_channel_id = channel_id
        resolved_model_id = model_id

    channel = get_channel(resolved_channel_id)

    if validate_catalog:
        # 渠道声明了 models 但未包含该 kind → 不支持该能力
        if channel.models and kind not in channel.models:
            registered = ", ".join(sorted(channel.models)) or "(无)"
            raise ValueError(f"渠道 {channel.id!r} 不支持能力 {kind!r}；已注册: {registered}")
        catalog = channel.model_ids(kind)
        if catalog and resolved_model_id not in catalog:
            raise ValueError(
                f"渠道 {channel.id!r} 未注册 {kind} 模型 {resolved_model_id!r}；"
                f"可选: {', '.join(catalog)}"
            )

    return channel, resolved_model_id
