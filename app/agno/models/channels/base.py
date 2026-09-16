"""渠道（Channel）定义：模型目录注册单元。

连接信息（api_key / base_url）不写死在渠道文件，只声明 Settings 字段名，
由 `client_kwargs_for_channel` 从 config 加载。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agno.models.types import ModelDriver, ModelKind


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    """一条模型渠道：驱动 + Settings 字段映射 + 模型 id 目录。"""

    id: str
    driver: ModelDriver
    # Settings 字段名（app.core.config.Settings），值从环境 / .env 加载
    api_key_setting: str = ""
    base_url_setting: str = ""
    # 各能力下允许使用的 model id（空元组表示不校验目录）
    models: dict[ModelKind, tuple[str, ...]] = field(default_factory=dict)
    description: str = ""

    def model_ids(self, kind: ModelKind) -> tuple[str, ...]:
        return self.models.get(kind, ())
