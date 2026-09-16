"""媒体获取状态机（图片/语音/视频）。

与 inbox 分开的原因：媒体有自己的生命周期（待取→就绪/失败/跳过）、自己的
限速域（下载是账号会话级风控资源，见 docs/platform-limits.md §6），与「入站
投递状态机」是两件事。1:1 指向 inbox 便于溯源。

媒体消息的 `inbox.content` 为 NULL：源参数在 `source`，文本占位由本表派生——
这样原始 XML 不可能泄漏进模型提示词或会话 JSON。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tortoise import fields, models

if TYPE_CHECKING:
    from app.models.inbox import Inbox

MEDIA_PENDING = "pending"
MEDIA_READY = "ready"
MEDIA_FAILED = "failed"
MEDIA_SKIPPED = "skipped"

MEDIA_IMAGE = "image"


class Media(models.Model):
    """一条待取/已取的媒体，1:1 挂在其入站消息上。"""

    id = fields.BigIntField(primary_key=True)
    # FK 类型由 Tortoise 在运行时注入；这里显式声明便于类型检查与 IDE 跳转
    inbox: fields.ForeignKeyRelation[Inbox] = fields.ForeignKeyField(
        "models.Inbox", unique=True, on_delete=fields.CASCADE, description="来源入站消息"
    )
    # Tortoise 在运行时为 FK 生成 <field>_id；显式声明便于类型检查（无赋值，不建列）
    inbox_id: int
    # 冗余账号/会话键：下载队列按账号限速与扫表都要用，避免 join
    self_wxid = fields.CharField(max_length=64)
    friend_wxid = fields.CharField(max_length=64)
    media_type = fields.CharField(max_length=16, description="image|voice|video")
    status = fields.CharField(
        max_length=16, default=MEDIA_PENDING, description="pending|ready|failed|skipped"
    )
    # 取媒体所需的原始参数（按类型不同）：
    #   image → {"xml": "<msg>...</msg>"}
    #   voice → {"xml": ..., "msg_id": ...}
    source: dict[str, Any] = fields.JSONField()
    # 取到的资源引用：上游 URL（P0）或自有存储路径；语音可放转码后的 mp3
    asset_ref = fields.TextField(null=True)
    # 媒体的文本化（如语音 ASR 结果），有值时可并入上下文
    text_repr = fields.TextField(null=True)
    attempt = fields.IntField(default=0, description="取用次数，不是重试策略")
    error_code = fields.CharField(max_length=64, null=True)
    error_message = fields.CharField(max_length=500, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "media"
        indexes = (
            ("status", "id"),
            ("self_wxid", "status", "id"),
            ("self_wxid", "friend_wxid", "status"),
        )
