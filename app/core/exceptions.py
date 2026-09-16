"""业务异常体系 + 全局异常处理器注册。

设计原则：
- AppException 是纯 Python Exception，不耦合 FastAPI 的 HTTPException。
- 异常处理器在应用边界将异常转换为统一 ApiResponse 格式。
- 异常日志由 middleware 层（LoggingMiddleware）自动记录，处理器只负责格式转换。
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.response import fail

# ── 异常基类 ────────────────────────────────────────────────────────────


class AppException(Exception):  # noqa: N818
    """业务异常基类。

    Attributes:
        code:        业务错误码（0 为成功，非 0 为业务错误）。
        message:     对人类可读的错误描述。
        status_code: HTTP 状态码，传递给响应。
    """

    def __init__(self, code: int = -1, message: str = "", status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


# ── 常见业务异常子类 ────────────────────────────────────────────────────


class NotFound(AppException):
    """资源不存在 (404)。"""

    def __init__(self, message: str = "资源不存在"):
        super().__init__(code=1001, message=message, status_code=404)


class BadRequest(AppException):
    """请求参数错误 (400)。"""

    def __init__(self, message: str = "请求参数错误"):
        super().__init__(code=1002, message=message, status_code=400)


class Unauthorized(AppException):
    """未授权 (401)。"""

    def __init__(self, message: str = "未授权"):
        super().__init__(code=1003, message=message, status_code=401)


class Forbidden(AppException):
    """无权限 (403)。"""

    def __init__(self, message: str = "无权限"):
        super().__init__(code=1004, message=message, status_code=403)


class UpstreamError(AppException):
    """上游依赖不可用 (502)。

    用于 GeWe / 百炼等外部服务超时、网络错误、网关 5xx。
    客户端可据此重试；业务侧错误仍用 BadRequest。
    """

    def __init__(self, message: str = "上游服务暂时不可用，请稍后重试"):
        super().__init__(code=2001, message=message, status_code=502)


# ── 全局异常处理器注册 ──────────────────────────────────────────────────


def _make_error_response(
    status_code: int, code: int, message: str, data: object = None
) -> JSONResponse:
    """构造统一格式的错误响应。"""
    return JSONResponse(
        status_code=status_code,
        content=fail(code=code, message=message, data=data).model_dump(),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """在 FastAPI 实例上注册所有全局异常处理器。"""

    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        return _make_error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return _make_error_response(exc.status_code, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        summary = first.get("msg", "请求参数校验失败")
        return _make_error_response(
            status_code=422,
            code=422,
            message=f"请求参数校验失败: {summary}",
            data=[{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in errors],
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """未预期异常兜底。异常日志由 middleware 层负责，这里只返回统一格式。"""
        return _make_error_response(500, 500, "服务器内部错误")
