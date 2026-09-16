# 生成层（Agno Agent）

落点：`app/agno/`（Agent 与模型）+ `app/utils/generation.py`（生成接缝）。
上下文组装见 `docs/architecture.md` §9；决策背景见 `docs/decisions.md` 0013。

## 分层与纪律

| 层 | 路径 | 职责 |
|----|------|------|
| 接缝 | `app/utils/generation.py` | `generate_text` / `LLMError`；**services 唯一入口** |
| Agent 层 | `app/agno/agents/` | 注册表 + 路由 + 人设（`reply.py`） |
| 模型层 | `app/agno/models/` | 渠道目录（`channels/`）+ 工厂（`llm/`）+ 生成参数常量 |

- **`services` / `crud` / `workers` 禁止直接 `import app.agno`**，只能经接缝。这是 §14.4「换生成实现只动一个文件」的落点。
- 接缝**不读数据库、不认识 inbox/outbox/GeWe**，只吃数据、只吐文本。

## 可核对事实

- 依赖：**`agno==3.0.9`**，**只装核心**，不装 `agno[os]`（那会拉进 fastapi/PyJWT/sqlalchemy/opentelemetry/mcp）。核心不含 sqlalchemy/greenlet/psycopg，与 tortoise-orm 无交集。
- 模型：官方 **`agno.models.dashscope.DashScope`**，不写包装子类。所有构造经 `app/agno/models/llm/builders.py` 收口。
- **`base_url` 必须显式传** `settings.DASHSCOPE_BASE_URL`：agno 默认打到**国际站** `dashscope-intl.aliyuncs.com`，国内 Key 会 401。
- **`enable_thinking` 必须显式设 `True`**：agno 的 `DashScope.enable_thinking` 默认 `False`，且 `get_request_params` **每次无条件**写 `extra_body`。不显式传就会相对旧实现（不传 = 百炼默认开启）静默关掉思考。**不要**再往 `request_params` 塞 `extra_body`（会被整键覆盖）。
- 模型 id 只在 `channels/bailian.py` 的目录里声明；**不进 env**。拼错由 `resolve_model` 白名单拦下。
- 生成参数（`temperature` 0.8 / `max_tokens` 4096 / 超时 300s）是 `builders.py` 的**常量**，不进 env。inbox 租约须 ≥ 超时 + 写库余量（默认 360s）。
- **`Agent` 不接收 `temperature` / `max_tokens` / `timeout`** —— 它们只是 model 层字段，传 Agent 会 `TypeError`。
- Agent 构造：`db=None`、`add_history_to_context=False`、`telemetry=False`、`retries=0`。**绝不挂 `learning`**（它会把 `add_history_to_context` 自动置 True，让框架接管历史）。
- **非流式 `arun` 不抛异常**：失败返回 `status=RunStatus.error` 且 `content=str(e)`。接缝必须显式判 `status`，否则错误文本会被当回复发出。已实测（坏端点 → `llm_unknown`）。
- **每轮显式传 `session_id`**（`f"{self_wxid}:{friend_wxid}"`）：不传时 agno 生成 UUID 并回写 Agent 实例（sticky），而实例按 agent_id 缓存、worker 长驻，会跨会话污染。
- 历史所有权在我们：`chat_sessions` 是唯一事实源；system prompt 由 Agent 的 `instructions` 提供，接缝会**剔除**消息里的 system 条目（否则会有两条且我们那条在后）。
- 失败语义：LLM 失败一律 inbox `failed` **终态，不重试**。一期 `status=error` 统一映射 `llm_unknown`，`llm_timeout` / `llm_empty` 保持精确。闭集见 `docs/architecture.md` §11.3。
- 禁止在 `utils` / `app/agno` 里重试或写库。

## 新增 Agent（每个微信号一个专属人设）

1. 在 `app/agno/agents/` 新增一个模块（照 `reply.py`）：`AGENT_ID` / `NAME` / `DESCRIPTION` / `INSTRUCTIONS` / `build_xxx_agent`
2. 在 `app/agno/agents/__init__.py` 的 `AGENTS` 注册表加一行
3. env 配 `NODE_AGENTS=本号wxid:agent_id,...`（未配则回落 `DEFAULT_AGENT_ID`）

不需要改 `services`、队列或表。`validate_agent_config()` 在 worker 启动时校验映射只引用已注册 id，拼错即启动失败（避免静默回落成错人设）。

模型缺省走渠道 `DEFAULTS`；要换模型传 `model=get_chat_model(model_id="...")`，要非常规配置就直接构造 `Agent(...)`。

下架名单（不要新用）：`qwen-turbo`、`deepseek-v3`、`deepseek-r1`、`qwen3.6-flash`。

## 现有 Agent

| 注册表 key | 人设 | skills |
|---|---|---|
| `reply` | 微信好友（默认） | 无 |

几条不可违反的约束：

- **不要给 `Agent(...)` 传 `temperature`/`max_tokens`/`timeout`**——它们是 model 层字段，传了 `TypeError`（决策 0013 事实 2）。生成参数只在 `app/agno/models/llm/builders.py` 收口。
- **输出上限是 `MODEL_MAX_TOKENS=4096`**。调小会**静默截断**且 `status` 仍 `completed`，极难发现。
- 新增 Agent 照 `reply.py` 五要素，**不要再造一套 Agent 构造器**。

## 长回复拆条（微信单条长度上限）

- 落点：纯函数 `app/utils/text_split.py` + `services/reply.py` 循环写 N 行 outbox。**发送层、限速域、DB 结构都不动**。
- **按 UTF-8 字节切，不按字数**（汉字 3 字节，差 3 倍）。`WECHAT_TEXT_MAX_BYTES=2000` 是保守值，**待真机实测校准**（GeWe 官方 `postText` 对 `content` 未给长度约束，超限行为未知）。
- **历史写完整文本**（`append_turn` 收到的是未拆分的原文）——这是「报告文本即图片的文本编码」得以成立、因而不需要图片回挂的前提。**不要改成写分段**。
- 顺序由 `outbox.id` 保证（同事务插入、`peek_next` 按 id 升序），不需要 `part_index` 列。
- **不做 Markdown 归一化**：决定原样发出，`##`/`**`/`![]()` 会字面显示。
- 段数超 `WECHAT_SPLIT_WARN_PARTS`(5) 只告警不截断——静默丢内容比多发几条更糟。

## 媒体（图片已落地，语音/视频待做）

落点：`app/models/media.py`（表）、`app/services/media.py`（取用+渲染）、`app/crud/media.py`（CRUD）、worker 的下载循环。平台红线见 `docs/platform-limits.md` §6。

### 数据结构约定

- 媒体**独立成 `media` 表**（1:1 挂 inbox），不往 inbox 加媒体列：媒体有自己的生命周期与限速域，混进队列表会互相干扰。
- **媒体消息的 `inbox.content` 为 NULL**，源 XML 存 `media.source`。原始 XML 因此**不可能**进模型提示词或 `chat_sessions`——这是结构保证，不靠「记得映射」。
- 三个类型共用 **`asset_ref`**（URL / 存储路径 / 转码产物）与 **`text_repr`**（语音 ASR 等文本化），新增类型不改表。
- **不做 handler/注册表抽象**：就三种媒体，写普通函数；新增语音 = 加一个 `_download_voice()` + `process_one` 里一个分支。

### 取用与限速（**红线**）

- **只有调 `downloadImage` 换 URL 这一步消耗账号会话**，所以它必须串行 + 每条 3~10s（`DOWNLOAD_INTERVAL_SECONDS`，**安全底线，不可放宽**）；频率高官方明确会掉线。
- 第 2 步（CDN GET 图片字节）是**百炼服务器**在做，与我们账号无关，无需限速。**不要**误解成我们要下载文件。
- 三层控制全按 `self_wxid`：间隔（3~10s）、日配额、pending 积压上限（超限标 `skipped`）。**不复用发送令牌桶。**
- 档位回退链 `2 常规 → 1 高清 → 3 缩略图`，三档全败才 `failed`。
- **生成侧 defer**：本会话有 `pending` 媒体时不认领（`peek_next_session` 的 `NOT EXISTS`），保证「发图求分析」带图。**媒体全部解析完之后还要按住静默窗**（`MEDIA_QUIET_MS=3000`），把下载期间用户补发的问题收进同一批——否则图文会被拆成两轮。
  - 静默窗覆盖「解析完成后用户补发的问题」，避免图文被拆成两轮。
  - 媒体窗只在 `DEBOUNCE_MS>0` 时启用：`debounce=0` 语义是「不设窗口」，此时加媒体窗会把静态的 `MAX(updated_at)` 当上限，导致会话卡死。
- **超时判定必须区分「排队」与「卡死」**：`fail_stale` 按 `attempt` 分两档（卡死 30s / 排队上限 420s）。只按 `created_at` 绝对年龄判死会让串行下载下第 6 张起就被误杀（有效容量从 30 张掉到约 5 张）。
- **媒体时间戳一律走 DB `NOW()`**（`mark_ready`/`mark_failed`/`mark_skipped`）：静默窗与超时判定都用 SQL 时钟，混入 Python 时钟会让钟差直接变成窗口误差。
- 媒体失败/跳过/超时 → 文本给「`[图片]`加载失败，无法查看」，并靠人设要求**只让对方重发、不猜内容**（防幻觉）。

### 传给模型

- 图片 URL 直接喂多模态模型（已实测：DashScope 接受 `http://` 明文与外域地址，无需取字节）。
- **图片必须挂在 user `Message` 上**（`Image(url=...)`），**不能**走 `arun(images=...)`：input 为 `list[Message]` 时 agno 会跳过构建 user 消息，`images` 被静默丢弃。
- 单轮最多 `MEDIA_MAX_PER_TURN`（2）张图；超出部分不静默丢弃，文本里注明「还有 N 张未展示」。

### 新增语音/视频

- **语音**：`downloadVoice`（需 `msgId`，放 `source`）+ silk→mp3 转码；mp3 落 `asset_ref`、ASR 结果落 `text_repr`。
- **视频**：`downloadVideo`（appId + xml）→ `asset_ref`。**注意**官方 agno `DashScope` 不支持视频 content 序列化，届时需补薄适配器。
- 两者都**不动表结构、不动队列、不动生成层**。
