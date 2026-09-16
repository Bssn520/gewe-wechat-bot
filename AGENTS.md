# Repository Guidelines

> GeWe 多微信账号自动回复服务。FastAPI（Python 3.13）+ Tortoise ORM + PostgreSQL；`uv` 管理依赖。一期无网页端、无 Celery、无 AgentOS。生成层用 Agno `Agent`（`app/agno/`），但只取 Agent 能力。
>
> **当前代码只有通用底座**（配置、日志、统一响应、健康检查、aerich）。自动回复业务尚未实现，不要在本文里写成已有模块路径。

**规范分层**

| 层 | 路径 | 何时 |
|----|------|------|
| Always-on | 本文 `AGENTS.md` | 每会话（红线 + 索引） |
| 领域语言 | `CONTEXT.md` | 改业务概念、命名、边界时 |
| 按需事实 | `.agents/rules/*.md` | 开始写对应业务时再 Read（勿 `@` 整份导入 always-on） |
| 架构详情 | `docs/architecture.md` | 落地回复管道时；**细节唯一出处**，rules 不重复 |
| 平台红线 | `docs/platform-limits.md` | 改限速 / 并发 / 多账号 / 上线前 |
| 环境与部署 | `docs/environments.md` | 配置、环境切换、部署 |
| 分期路线 | `docs/roadmap.md` | **仅人读**；禁止当 agent 实施入口 |
| 决策记录 | `docs/decisions.md` | 解释「为什么这样」；禁止当实施清单 |

Claude Code：`CLAUDE.md` 仅 `@AGENTS.md`。领域细节靠文末索引打开 rules，不依赖 skill 作为规范入口。

## 命令

```bash
uv sync --group dev
cp .env.example .env          # 首次；运行时只读 .env（+ 进程环境变量）
make migrate                  # 首次/改模型后
make dev                      # API :8000
make test && make lint        # lint = ruff check + format --check
make format                   # ruff format
make typecheck                # mypy app
make precommit                # 安装 git 钩子（ruff check --fix + format）
```

`APP_ENV` 仅 `dev` | `test` | `prod`。运行时只读 `.env` + 进程环境变量；`.env.dev` / `.env.test` / `.env.prod` 是存档，不自动加载。密钥只进环境变量或 `.env`，不写入 AGENTS / 文档。详见 `docs/environments.md`。

## 现有代码

- 入口：`app/main.py`（FastAPI + LoggingMiddleware + 异常处理 + Tortoise）
- HTTP：`app/api/v1/health.py`（`GET /api/v1/health`）
- 配置 / 日志 / 异常：`app/core/config.py`、`logging.py`、`exceptions.py`
- 统一响应：`app/schemas/response.py`（`{code, message, data}`）
- 生成层：`app/utils/generation.py`（接缝）+ `app/agno/`（Agent、模型渠道 `models/`；**只有接缝能 import**）
- 长回复拆条：`app/utils/text_split.py`（纯函数，编排层调用；见决策 0014）
- 媒体接收：`app/models/media.py` + `app/services/media.py`（取用与渲染）+ worker 下载循环
- 回复管道：`app/api/v1/webhook.py` → `app/services/` → `app/crud/` / `app/utils/`；worker 在 `app/workers/reply.py`

**尚未存在**：管理后台、Celery、语音/视频接收（图片已落地）。

## 分层（业务落地时遵守）

| 层 | ✅ | ❌ |
|----|----|----|
| Router | Schema、Depends、调 Service | ORM/CRUD、业务判断、`HTTPException` 业务错误、外呼 |
| Service | 规则、调 CRUD/外呼、`AppException` | 直写 ORM、定义路由、静默吞异常 |
| CRUD | filter/create/update，返回 Model/`None` | 业务判断、返回 Schema、调 Service |
| Model/Schema | Model 仅字段、索引与约束；Schema 用具体 Pydantic DTO | ORM 当响应；HTTP 边界裸 `dict`/`list`/`Any` |
| utils | 无状态 HTTP 客户端 | 业务规则 |
| workers | 调 Service | 直接 CRUD、解析 HTTP |

调用方向单向：Router → Service → CRUD / utils。Worker → Service。

自有 HTTP 接口用 `{code, message, data}`。业务异常继承 `AppException`。健康检查走统一响应壳。将来 GeWe webhook 成功响应必须是**空 body**，不要包这层壳。

| code | HTTP | 场景 |
|------|------|------|
| 0 | 200 | 成功 |
| 1001 | 404 | 不存在 |
| 1002 | 400 | 参数/业务 |
| 1003 | 401 | 未授权 |
| 1004 | 403 | 无权限 |
| 2001 | 502 | 上游故障 |
| 422/500 | — | 校验 / 未处理 |

## 编码

- Python 3.13；ruff（line-length=100）；I/O 全异步；配置只走 `app.core.config.settings`
- 内部时刻 **aware UTC**（`utcnow()` / `as_utc()`）；禁止 `.replace(tzinfo=None)`；租约比较走 SQL，日配额「今天」走 `QUOTA_TZ`。细节 `docs/architecture.md` §5.8
- 会话历史在 `chat_sessions`（一行 JSON）；库 `CHAT_STORE_ROUNDS=200`，模型 `REPLY_HISTORY_ROUNDS=20`
- `snake_case` / `PascalCase` / `UPPER_SNAKE`；日志 `structlog`，事件 `domain.action`
- 新 Model 更新 `app/models/__init__.py` 的 `__all__`

## Git

`type(scope): 中文描述`（feat/fix/refactor/chore/docs/test）。
分支：`main` / `dev` / `feature|fix|chore/<topic>`。从 `dev` 拉；合回 `dev` 用 **`git merge --no-ff`**；不 force-push `main`/`dev`。不擅自 commit / push。

## 数据库

Tortoise + aerich + PostgreSQL；**不要** `generate_schemas=True`。改模型 → migrate-make → migrate → 测。
不要设 `DATABASE_URL` env，只填 `DB_*`。测试库用独立 Postgres（如 `gewe_test`），不要连 `gewe_dev` / `gewe_prod`。

## 业务红线

回复链路：`webhook → inbox → 生成 → outbox → 发送`。细节见 `docs/architecture.md`。原则：

- webhook 鉴权后立刻空 200，落库走 BackgroundTasks；禁止在回调里 await LLM / 发送
- worker 内**两个循环**：生成可并发不同会话（先准入再认领）；发送每 `self_wxid` 严格串行、账号间并行。节点从回调学习
- inbox 认领**按会话批量**，不按单条。LLM 失败 → `failed` 终态不重试
- 限速按 `RateDomain` 拆成**准入**（生成前：熔断 / 日配额 / 积压）和**发放**（先等再认领：好友窗口集合 ≤10/分钟 + 令牌桶兜底 + 间隔）。Service 只 `admit` / `wait_to_dispatch`，闸门在 `core/rate_limit/`
- 队列表 `processing`/`sending` 带 `leased_until` **和** `lease_gen`；回收抬代次，标成功必须 CAS 对上
- 组装上下文只取 `delivered=true`；未送达 assistant 不入下一轮
- **生成层只吃数据、只吐文本**；换模型只改生成那一处。`services`/`crud`/`workers` **禁止直接 `import app.agno`**，只能经 `app/utils/generation.py` 接缝
- **长回复由编排层拆成多条 outbox 行**（`app/utils/text_split.py`，按 UTF-8 字节）；**历史里存完整原文**。发送层与限速不动，顺序由 `outbox.id` 保证（决策 0014）
- 默认人设是 `reply`。新增 Agent 照 `reply.py` 加模块 + 注册表一行 + `NODE_AGENTS` 映射，不要改队列或 services
- 一期不上 Celery、不上 AgentOS（不用其 session/memory/路由）、不做群回复、不做养号、**不做账号生命周期**（登录/重连/解封在 GeWe 控制台；管道只消费掉线回调和发送错误码）
- HTTP 业务错误走已有 `AppException`（1001–1004 / 2001）；队列表失败走内部字符串 `error_code`（§11.3）。禁止 `HTTPException` 表达业务错误，禁止比对 GeWe 中文文案
- 密钥不入仓；禁止 SQLite 当业务库

详情（规划，不是现有文件）：`.agents/rules/webhook-inbound.md`、`async-outbox.md`、`llm-generation.md`、`accounts-allowlist.md`

## 边界

✅ 健康检查；`AppException`；测后 lint+pytest

⚠️ 先问：新 pip、Celery、公开管理 API、群聊、AgentOS

🚫 密钥入仓；Router ORM / 业务 `HTTPException`；`generate_schemas=True`；同步堵 I/O；SQLite 当业务库；把未实现路径写进代码 import

## 测试

`make test` / `make lint`。DB：`APP_ENV=test` + 独立 Postgres（如 `gewe_test`），不要连 `gewe_dev`。

## 何时读哪份

| 改动范围 | 打开 |
|----------|------|
| 配置 / 环境 / 部署 | `docs/environments.md` |
| 分层 / 异常 / Schema | 本文 + `docs/decisions.md`（0001）|
| 实现回复管道（回调 / 生成 / 发送）| `docs/architecture.md` + 对应 rule + `docs/decisions.md`（0005/0006/0007/0008/0009）|
| **改生成 / 换模型 / 加 Agent 人设** | **`.agents/rules/llm-generation.md` + `docs/decisions.md`（0013）** |
| **改媒体接收 / 下载限速** | **`docs/platform-limits.md` §6（必读）** + `docs/architecture.md` §5.5 |
| **改限速 / 并发 / 多账号 / 上线** | **`docs/platform-limits.md`（平台限制与封号红线，必读）** |
| 开始做回调 / 出站 / LLM / 白名单 | 对应 `.agents/rules/*.md` |

先改代码与本文红线，再同步对应 rule。rule 只写落点与可核对事实；**不**粘贴 `docs/roadmap.md`。
