from __future__ import annotations

from tortoise import fields, models


class Node(models.Model):
    """本号 wxid ↔ 当前 GeWe 槽。熔断两列挂在行上，不是账户中心。"""

    id = fields.BigIntField(primary_key=True)
    self_wxid = fields.CharField(max_length=64, unique=True, description="节点微信号")
    current_app_id = fields.CharField(max_length=64, unique=True, description="当前 GeWe 设备 ID")
    circuit_open_until = fields.DatetimeField(null=True)
    circuit_reason = fields.CharField(max_length=64, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "nodes"
