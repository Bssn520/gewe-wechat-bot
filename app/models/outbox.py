from __future__ import annotations

from tortoise import fields, models

from app.core.queue_constants import OUTBOX_PENDING


class Outbox(models.Model):
    """出站发送状态机。"""

    id = fields.BigIntField(primary_key=True)
    self_wxid = fields.CharField(max_length=64, description="节点微信号")
    app_id = fields.CharField(max_length=64, null=True, description="认领时写入的当前槽")
    friend_wxid = fields.CharField(max_length=64)
    content = fields.TextField()
    source_inbox_ids: list[int] | None = fields.JSONField(null=True)
    status = fields.CharField(max_length=16, default=OUTBOX_PENDING)
    attempt = fields.IntField(default=0)
    lease_gen = fields.IntField(default=0)
    leased_until = fields.DatetimeField(null=True)
    error_code = fields.CharField(max_length=64, null=True)
    error_message = fields.CharField(max_length=500, null=True)
    external_id = fields.CharField(max_length=64, null=True, description="GeWe newMsgId 字符串")
    next_retry_at = fields.DatetimeField(null=True)
    sending_at = fields.DatetimeField(null=True)
    sent_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "outbox"
        indexes = (
            ("self_wxid", "status", "id"),
            ("status", "leased_until"),
            ("self_wxid", "sent_at"),
            ("next_retry_at",),
        )
