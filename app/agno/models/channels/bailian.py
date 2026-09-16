"""阿里云百炼渠道（模型目录注册）。

连接配置从 Settings 加载（勿写死在本文件）：
- DASHSCOPE_API_KEY
- DASHSCOPE_BASE_URL

本文件只维护：渠道 id、driver、模型目录。**模型 id 的唯一声明处**——不进 env，
拼错由 registry 的白名单校验拦下。
"""

from __future__ import annotations

from app.agno.models.channels.base import ChannelSpec

CHANNEL = ChannelSpec(
    id="bailian",
    # driver 与渠道 id 分开命名：id "bailian" 是厂商，driver 是本项目的适配分支
    driver="dashscope",
    api_key_setting="DASHSCOPE_API_KEY",
    base_url_setting="DASHSCOPE_BASE_URL",
    description="阿里云百炼（vlm；适配器=官方 agno DashScope）",
    models={
        # 多模态（可纯文本）。qwen3.8-flash 属多模态族，图片分析必需
        "vlm": ("qwen3.8-flash",),
    },
)
