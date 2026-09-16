from __future__ import annotations

from tortoise import fields, models

from app.core.queue_constants import INBOX_PENDING


class Inbox(models.Model):
    """入站投递状态机。群/验证包/系统事件不落库。"""

    id = fields.BigIntField(primary_key=True)
    self_wxid = fields.CharField(max_length=64, description="节点微信号")
    app_id = fields.CharField(max_length=64, description="当次 GeWe 槽快照")
    friend_wxid = fields.CharField(max_length=64, description="好友 wxid")
    new_msg_id = fields.CharField(max_length=64, description="GeWe newMsgId，字符串")
    content = fields.TextField(
        null=True, description="文本；媒体消息为 NULL，源参数在 media.source"
    )
    status = fields.CharField(
        max_length=16, default=INBOX_PENDING, description="pending|processing|done|failed"
    )
    attempt = fields.IntField(default=0, description="认领次数，不是 LLM 重试")
    lease_gen = fields.IntField(default=0, description="租约代次")
    leased_until = fields.DatetimeField(null=True)
    claimed_at = fields.DatetimeField(null=True, description="首次认领时刻，观测排队")
    worker_id = fields.CharField(max_length=64, null=True)
    error_code = fields.CharField(max_length=64, null=True)
    error_message = fields.CharField(max_length=500, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "inbox"
        unique_together = (("self_wxid", "new_msg_id"), ("app_id", "new_msg_id"))
        indexes = (
            ("status", "id"),
            ("self_wxid", "friend_wxid", "status", "id"),
            ("status", "leased_until"),
        )
