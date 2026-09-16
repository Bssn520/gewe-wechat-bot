from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GeWeCallback(BaseModel):
    """GeWe 回调 DTO。字段名跟官方 JSON 走，允许缺字段。"""

    model_config = ConfigDict(extra="ignore")

    appid: str | None = None
    wxid: str | None = None
    content: str | None = None
    from_user: str | None = Field(default=None, alias="fromUser")
    to_user: str | None = Field(default=None, alias="toUser")
    is_self: bool | None = Field(default=None, alias="isSelf")
    msg_id: int | str | None = Field(default=None, alias="msgId")
    new_msg_id: int | str | None = Field(default=None, alias="newMsgId")
    msg_type: str | None = Field(default=None, alias="msgType")
