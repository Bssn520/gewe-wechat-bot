from __future__ import annotations

from app.core.rate_limit.domain import RateDomain
from app.core.rate_limit.plan import RatePlan

_PLAN = RatePlan()


def for_account(self_wxid: str, domain: RateDomain) -> RatePlan:
    if domain is not RateDomain.OUTBOUND_TEXT:
        raise ValueError(f"只支持 {RateDomain.OUTBOUND_TEXT}，拒绝 {domain}")
    _ = self_wxid
    return _PLAN
