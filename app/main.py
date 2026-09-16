from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from tortoise.contrib.fastapi import RegisterTortoise

from app.api.v1.health import router as health_router
from app.api.v1.webhook import router as webhook_router
from app.core.config import TORTOISE_ORM, settings
from app.core.exceptions import NotFound, register_exception_handlers
from app.core.logging import configure_logging
from app.middleware import LoggingMiddleware

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    logger.info(
        "app.starting",
        app=settings.APP_NAME,
        version=settings.APP_VERSION,
        env=settings.APP_ENV,
    )
    if settings.is_test:
        logger.info("app.started", mode="test", database="managed_by_pytest")
        yield
        logger.info("app.stopped")
        return

    async with RegisterTortoise(
        app,
        config=TORTOISE_ORM,
        generate_schemas=False,
        add_exception_handlers=False,
    ):
        logger.info("app.started", db_url=settings.DATABASE_URL)
        yield
    logger.info("app.stopped")


def _register_project_routers(app: FastAPI) -> None:
    app.include_router(health_router, prefix=settings.API_V1_PREFIX)
    app.include_router(webhook_router)


def _register_catch_all(app: FastAPI) -> None:
    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )
    async def catch_all_404() -> None:
        raise NotFound(message="请求的接口不存在")


def create_app() -> FastAPI:
    application = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )

    application.add_middleware(LoggingMiddleware)
    register_exception_handlers(application)
    _register_project_routers(application)
    _register_catch_all(application)
    return application


configure_logging(json_log=settings.is_prod, log_level="DEBUG" if settings.DEBUG else "INFO")

app = create_app()
