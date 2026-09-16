"""健康检查端点。"""

from typing import Any

from fastapi import APIRouter

from app.core.config import settings
from app.schemas.response import ApiResponse, success

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> ApiResponse[dict[str, Any]]:
    """系统健康检查。"""
    return success(
        data={
            "status": "ok",
            "app": settings.APP_NAME,
            "env": settings.APP_ENV,
            "version": settings.APP_VERSION,
        }
    )
