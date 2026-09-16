"""应用统一日志配置。

使用 structlog 实现结构化日志：
- 开发环境（local）：控制台彩色输出，人类友好
- 生产环境（prod）：JSON 输出，供日志平台消费
- 同时接管 Uvicorn、Tortoise ORM 等标准库日志，统一格式

用法（每个模块顶部）：
    import structlog
    logger = structlog.get_logger(__name__)
    logger.info("user.created", user_id=42, source="wechat")
"""

import logging
import re
import sys

import orjson
import structlog
from structlog.stdlib import ProcessorFormatter

from app.core.config import settings

_DATABASE_URL_PATTERN = re.compile(
    r"\b(postgres(?:ql)?(?:\+[a-z0-9_.-]+)?://)[^\s'\"<>\[\]{}(),]+",
    flags=re.IGNORECASE,
)


def _redact_database_url_value(value: object) -> object:
    """递归脱敏日志载荷中的 PostgreSQL 连接串。"""
    if isinstance(value, str):
        return _DATABASE_URL_PATTERN.sub(r"\1***", value)
    if isinstance(value, dict):
        return {key: _redact_database_url_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_database_url_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_database_url_value(item) for item in value)
    return value


def redact_database_urls(
    _logger: logging.Logger,
    _method_name: str,
    event_dict: dict[str, object],
) -> dict[str, object]:
    """统一隐藏结构化字段和普通日志消息中的 PostgreSQL 连接信息。"""
    for key, value in event_dict.items():
        event_dict[key] = _redact_database_url_value(value)
    return event_dict


# 匹配 query string 中的敏感参数键及其值。键前必须有 ? 或 &，保证只命中 query 参数，
# 不误伤 JSON 键、断言文本等普通出现。值边界用 [^&\s"']* 截断，避免吞掉后续参数。
_QUERY_SECRET_PATTERN = re.compile(
    r"([?&])(access_token|refresh_token|token|secret|api_key|apikey|password|signature|sig)(=)[^&\s\"']*",
    flags=re.IGNORECASE,
)


def _redact_query_value(value: object) -> object:
    """递归脱敏日志载荷中的 query 敏感参数（token/secret 等）。"""
    if isinstance(value, str):
        return _QUERY_SECRET_PATTERN.sub(r"\1\2\3***", value)
    if isinstance(value, dict):
        return {key: _redact_query_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_query_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_query_value(item) for item in value)
    return value


def redact_query_secrets(
    _logger: logging.Logger,
    _method_name: str,
    event_dict: dict[str, object],
) -> dict[str, object]:
    """统一隐藏结构化字段和普通日志消息中 query string 的敏感参数。

    覆盖 uvicorn access log（message 是含 query 的完整请求行）及任何嵌套字段。
    """
    for key, value in event_dict.items():
        event_dict[key] = _redact_query_value(value)
    return event_dict


class _HealthAccessFilter(logging.Filter):
    """Docker 存活探测走 uvicorn.access，不记业务访问日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return f"GET {settings.API_V1_PREFIX}/health" not in message


def configure_logging(json_log: bool = False, log_level: str = "INFO") -> None:
    """配置全局日志（应用启动时调用一次）。"""

    # ── 1. structlog 处理链（所有日志共享） ────────────────────────────
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        redact_database_urls,
        redact_query_secrets,
    ]

    # ── 2. 配置 structlog 自身 ─────────────────────────────────────────
    structlog.configure(
        processors=shared_processors + [ProcessorFormatter.wrap_for_formatter],  # type: ignore[arg-type]
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ── 3. 输出格式处理器 ──────────────────────────────────────────────
    _orjson_serializer = lambda o, **_: orjson.dumps(o).decode()  # noqa: E731

    renderer = (
        structlog.processors.JSONRenderer(serializer=_orjson_serializer)
        if json_log
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    formatter = ProcessorFormatter(
        processors=[
            ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,  # type: ignore[arg-type]
    )

    # ── 4. 统一接管 stdlib 日志体系 ─────────────────────────────────────
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level.upper())
    root.handlers.clear()
    root.addHandler(handler)

    logging.getLogger("uvicorn").handlers.clear()
    logging.getLogger("uvicorn").propagate = True

    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = True
    logging.getLogger("uvicorn.access").addFilter(_HealthAccessFilter())

    logging.getLogger("uvicorn.error").handlers.clear()
    logging.getLogger("uvicorn.error").propagate = True

    logging.getLogger("tortoise").setLevel("WARNING" if log_level.upper() != "DEBUG" else "DEBUG")
    logging.getLogger("httpx").setLevel(logging.WARNING)
