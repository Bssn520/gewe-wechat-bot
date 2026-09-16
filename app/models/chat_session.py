from __future__ import annotations

from tortoise import fields, models


class ChatSession(models.Model):
    """本号↔好友一行会话。messages 是 JSON 数组，组装只取 delivered=true 的尾部。"""

    id = fields.BigIntField(primary_key=True)
    self_wxid = fields.CharField(max_length=64)
    friend_wxid = fields.CharField(max_length=64)
    messages: list = fields.JSONField(default=list)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "chat_sessions"
        unique_together = (("self_wxid", "friend_wxid"),)
