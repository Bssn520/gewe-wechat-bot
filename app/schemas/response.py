"""统一 API 响应模型。

所有接口返回值必须遵循 {code, message, data} 格式约定。
成功时使用 success()，失败时使用 fail() 或直接抛出 AppException。
GeWe webhook 成功响应是空 body，不要走本模块。
"""

from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse[T](BaseModel):
    """统一 API 响应结构。

    Attributes:
        code:    业务码，0 表示成功，非 0 表示业务错误。
        message: 对人类可读的描述信息。
        data:    响应载荷，可为 None。
    """

    code: int = 0
    message: str = "ok"
    data: T | None = None


def success[T](data: T | None = None, message: str = "ok") -> ApiResponse[T]:
    """构造成功响应（code = 0）。"""
    return ApiResponse(code=0, message=message, data=data)


def fail[T](code: int = -1, message: str = "error", data: T | None = None) -> ApiResponse[T]:
    """构造失败响应（code ≠ 0）。"""
    return ApiResponse(code=code, message=message, data=data)
