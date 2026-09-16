from functools import lru_cache
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置，统一从环境变量 / .env 读取。"""

    # 运行时只读 .env（及进程环境变量；后者优先）。
    # .env.dev / .env.test / .env.prod 仅为多环境配置存档，不由此处加载；
    # 切换环境请 cp 对应文件为 .env，或由部署平台直接注入环境变量。
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- 应用 ---
    APP_NAME: str = "gewe-wechat-bot"
    APP_VERSION: str = "0.1.0"
    APP_ENV: Literal["dev", "test", "prod"] = "dev"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # --- 服务 ---
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # --- 数据库（PostgreSQL；拆开声明，由属性组装为连接串）---
    # 注意：没有 DATABASE_URL 环境变量字段；连接串只能通过 DB_* 或下方 property 得到。
    # 单元测试不要设置 DATABASE_URL 环境变量。
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "postgres"
    DB_PASSWORD: str = "postgres"
    DB_NAME: str = "gewe_dev"

    # --- 百炼（OpenAI 兼容；密钥只进 env / .env）---
    # 模型 id 与生成参数（temperature / max_tokens / thinking / 超时）是
    # app/agno/models/ 里的常量，不进 env——避免「代码目录」与「环境变量」两个事实源。
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    REPLY_HISTORY_ROUNDS: int = 20
    CHAT_STORE_ROUNDS: int = 200

    # --- Agent 路由（执行节点 → Agent）---
    # 空 = 全部走默认 Agent。非空 = 本号wxid:agent_id,...（agent_id 必须在注册表中）
    NODE_AGENTS: str = ""

    # --- GeWe ---
    GEWE_BASE_URL: str = "http://api.geweapi.com"
    GEWE_TOKEN: str = ""
    GEWE_ALLOWLIST: str = ""
    WEBHOOK_SECRET: str = ""
    GEWE_HTTP_TIMEOUT: float = 20.0

    # --- 生成 / 准入 ---
    INBOX_CONCURRENCY_GLOBAL: int = 6
    INBOX_CONCURRENCY_PER_ACCOUNT: int = 2
    INBOX_BATCH_MAX: int = 10
    DEBOUNCE_MS: int = 10000
    OUTBOX_BACKLOG_PER_ACCOUNT: int = 20
    # 须覆盖 GENERATION_TIMEOUT_SECONDS + 写库余量（约 60s）；见 docs/architecture.md §12
    INBOX_LEASE_SECONDS: int = 360
    INBOX_LEASE_GRACE_SECONDS: int = 30

    # --- 发放（每 self_wxid + OUTBOUND_TEXT）---
    SEND_MAX_DISTINCT_FRIENDS_PER_MINUTE: int = 10
    SEND_MAX_PER_MINUTE: int = 40
    SEND_BURST: int = 2
    SEND_DAILY_CAP: int = 200
    SEND_INTERVAL_SAME_MS: str = "1500-3500"
    SEND_INTERVAL_DIFF_MS: str = "4000-8000"
    SEND_LEASE_SECONDS: int = 60
    SEND_LEASE_GRACE_SECONDS: int = 30
    SEND_MAX_ATTEMPTS: int = 2
    QUOTA_TZ: str = "Asia/Shanghai"

    # worker 轮询
    WORKER_POLL_SECONDS: float = 1.0
    RECLAIM_INTERVAL_SECONDS: float = 15.0

    @property
    def DATABASE_URL(self) -> str:  # noqa: N802
        """由 DB_* 字段组装的 PostgreSQL 连接串（非环境变量，不可被 DATABASE_URL env 覆盖）。"""
        return (
            f"postgres://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV == "dev"

    @property
    def is_test(self) -> bool:
        return self.APP_ENV == "test"

    @property
    def is_prod(self) -> bool:
        return self.APP_ENV == "prod"

    @property
    def allowlist_enabled(self) -> bool:
        """空字符串 = 白名单关闭。"""
        return bool((self.GEWE_ALLOWLIST or "").strip())

    @property
    def allowlist_by_wxid(self) -> dict[str, frozenset[str]]:
        """GEWE_ALLOWLIST=本号wxid:好友|好友,... → {self_wxid: frozenset(friend)}。"""
        result: dict[str, frozenset[str]] = {}
        raw = (self.GEWE_ALLOWLIST or "").strip()
        if not raw:
            return result
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            self_wxid, friends = chunk.split(":", 1)
            self_wxid = self_wxid.strip()
            wxids = frozenset(w.strip() for w in friends.split("|") if w.strip())
            if self_wxid and wxids:
                result[self_wxid] = wxids
        return result

    @property
    def agents_by_wxid(self) -> dict[str, str]:
        """NODE_AGENTS=本号wxid:agent_id,... → {self_wxid: agent_id}。

        空串 = 全部走默认 Agent。非法项（缺冒号、空 id）跳过，不在此处抛错；
        agent_id 是否已注册由 app.agno.agents.validate_agent_config 在启动时校验。
        """
        result: dict[str, str] = {}
        raw = (self.NODE_AGENTS or "").strip()
        if not raw:
            return result
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            self_wxid, agent_id = chunk.split(":", 1)
            self_wxid = self_wxid.strip()
            agent_id = agent_id.strip()
            if self_wxid and agent_id:
                result[self_wxid] = agent_id
        return result

    @property
    def send_interval_same_s(self) -> tuple[float, float]:
        return _parse_ms_range(self.SEND_INTERVAL_SAME_MS)

    @property
    def send_interval_diff_s(self) -> tuple[float, float]:
        return _parse_ms_range(self.SEND_INTERVAL_DIFF_MS)

    @model_validator(mode="after")
    def _prod_must_not_debug(self) -> Self:
        if self.is_prod and self.DEBUG:
            raise ValueError("prod 禁止 DEBUG=true")
        return self

    @model_validator(mode="after")
    def _prod_requires_secrets(self) -> Self:
        if not self.is_prod:
            return self
        missing = [
            name
            for name, val in (
                ("DASHSCOPE_API_KEY", self.DASHSCOPE_API_KEY),
                ("GEWE_TOKEN", self.GEWE_TOKEN),
                ("WEBHOOK_SECRET", self.WEBHOOK_SECRET),
                ("DB_PASSWORD", self.DB_PASSWORD),
            )
            if not (val or "").strip()
        ]
        if missing:
            raise ValueError("prod 配置不完整，缺少: " + ", ".join(missing))
        return self


def _parse_ms_range(raw: str) -> tuple[float, float]:
    """'1500-3500' → (1.5, 3.5) 秒。"""
    text = (raw or "").strip()
    low_s, _, high_s = text.partition("-")
    low = int(low_s)
    high = int(high_s or low_s)
    if high < low:
        low, high = high, low
    return low / 1000.0, high / 1000.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Tortoise ORM 配置，供 app 启动和 aerich 迁移工具读取
TORTOISE_ORM = {
    "connections": {"default": settings.DATABASE_URL},
    "apps": {
        "models": {
            "models": ["app.models", "aerich.models"],
            "default_connection": "default",
        }
    },
    "use_tz": True,
    "timezone": "UTC",
}
