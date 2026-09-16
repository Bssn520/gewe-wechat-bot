from __future__ import annotations

from dataclasses import dataclass

from app.core.rate_limit.domain import RateDomain


@dataclass(frozen=True, slots=True)
class SendIntent:
    self_wxid: str
    friend_wxid: str
    domain: RateDomain = RateDomain.OUTBOUND_TEXT
