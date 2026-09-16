# 决策记录

> 本项目的重要决定与**为什么**。对应约定里的 ADR，但合成一份便于通读——项目还小，每条决策不值得单开一个文件。
> **新增决策**：在文末追加 `## 00NN: 标题` 一节，编号递增，不修改历史条目。
> 只记「为什么」。**怎么做**在 [`architecture.md`](./architecture.md)；**落点**在 `.agents/rules/`。

---

## 0001: Router → Service → CRUD 三层分层

统一分层，避免业务逻辑散落在路由、ORM 和外呼里。

调用单向：Router → Service → CRUD / utils。Worker 只调 Service。Webhook 成功响应是空 body，不走 `{code,message,data}`；自有健康检查等接口仍用统一响应壳。业务异常继承 `AppException`，禁止用 `HTTPException` 表达业务错误。

## 0002: 一期不上 Agent 框架

> ⚠️ **已被 0013 部分取代**：生成实现现用 Agno `Agent`。但本文另一半结论仍然有效——**不把 AgentOS / Pi 当 GeWe 入口**，也不让任何框架接管入站与发送两层。

一期需求是个人好友纯文本多轮回复。一次 OpenAI 兼容 `chat/completions` 加自管会话即可。Agno / LangChain / LangGraph / Pi 解决不了 GeWe 的 3 秒 ACK、去重和出站串行，却会把 webhook 语义埋进 runtime。

工具调用、MCP、skills 等 Agent 循环放到后期单独调研。届时只改「生成」那一步；按 `architecture.md` §14 的边界，入站与发送两层不应需要改动。禁止把 AgentOS 或 Pi 当 GeWe 入口。

## 0003: 一期用 inbox/outbox 表，不上 Celery

「回调只投递、发送不在 HTTP 线程」这个语义要。实现用数据库 inbox/outbox + asyncio worker，表是真相源。

Celery 适合多机水平扩展和多类长任务；一期单机、私聊、按账号串行限速，再引入 Redis broker 和默认并发，会打穿 GeWe 风控。切换条件：多机 worker、多种 job、或 API 与推理必须分进程崩溃隔离。到时任务只传记录 id，限速仍走 outbox。

## 0004: 一期只回个人好友白名单

个微协议非官方，群高频同质化回复是封号与骚扰双坑。一期识别 `@chatroom` 即丢弃，不进 LLM、不写 outbox。私聊必须命中该执行节点的好友白名单才回复。群自动回复若做，另开调研，默认 @ 才回，且不预埋在一期发送路径里。

## 0005: worker 分「生成循环」与「发送循环」，并发语义相反

回调链路定为 `webhook → inbox → 生成 → outbox → 发送`。落地时 worker 内跑两个独立循环，**不合并**：

- **生成循环**消费 inbox（调 LLM 产出文本），可并发**不同会话**，上限按执行节点封顶（2–3）并有全局闸门。
- **发送循环**驱动 outbox，**每个执行节点严格 1 条 in-flight**，叠加令牌桶与间隔抖动。

理由是二者性质相反：生成只是到百炼的 HTTP 请求，**对外不可见**；发送真正动了微信，**对外可见且不可撤回**，超时无法确认是否已送达，只能有限重试。崩溃回收退回 `pending` 是恢复，不是「失败后再跑」；LLM 一旦调用失败，inbox 进 `failed` 终态、不重试。把生成也压成串行不会让账号更「像人」（可见节奏由发送间隔决定），只会让第 n 个好友多等 `n × LLM 延迟`，并把一次 LLM 卡顿放大为所有人的阻塞。

inbox 认领采用**按会话批量**（`GROUP BY self_wxid, friend_wxid` + `FOR UPDATE SKIP LOCKED` 认领该会话全部 pending），而非按单条。这一处同时得到：同会话串行（无需 per-session 锁，天然多进程安全）、连续消息合并、批内顺序正确。

限速按 `RateDomain` 拆成**准入**（生成前）和**发放**（先等再认领），不写进 outbound 一段 inline 流程。**「串行」的作用域是按 `self_wxid`：账号内串行、账号间并行。** 风控按微信号判定，A 号发得快不影响 B 号，全局串行是零安全收益 + 纯延迟代价。取值 SQL 的 `NOT EXISTS` 必须带 `x.self_wxid = o.self_wxid`。反向地，**worker 实例必须全局唯一**——内存桶每进程一份，两个实例等于速率翻倍；worker 因此独立于 API 进程，并在启动时抢 `pg_try_advisory_lock` 锁死单实例。不要把 worker 放进 FastAPI 的 lifespan：`uvicorn --workers N` 会让每个节点出现 N 个限速器。API 可水平扩。

细节见 `architecture.md`。

### Considered Options

- **单个循环串行处理（生成与发送合一）**：最简，但 LLM 延迟直接累加进发送节奏，且无法对「可安全重试」与「不可轻易重试」采用不同策略，只能取折中的错误值。
- **生成也按节点串行**：曾考虑「一个账号同一时刻只回一个好友」，但该约束描述的是**发送**而非生成。设成 1 会让延迟随好友数线性增长而换不到任何安全性。
- **不设 outbox，生成后直接发**：则慢的限速发送会阻塞生成，发送失败重试还必须重跑 LLM。
- **轮询间隔改为 `LISTEN/NOTIFY` 驱动**：低频下无必要，轮询间隔本身就是节流的一部分。保持表为事实来源 + 轮询。
- **worker 与 API 同进程（放进 lifespan）**：队列已在 Postgres，同进程省不掉任何 DB 往返；却会被 `uvicorn --workers N` 放大限速、让 3 秒 ACK 与 worker 健康绑定、并让健康检查被卡住的 LLM 调用饿死。
- **发送全局串行（所有账号共用一条通道）**：风控按微信号判定，跨账号串行纯属自找延迟。

## 0006: 队列字段与状态机纪律，但不引入 Celery/registry 层

inbox / outbox 的字段设计与状态机写法沿用常见的 `jobs` 队列纪律（状态机、租约、错误码分列），不引入 Celery / registry 执行平面。

**照抄的四条纪律**

1. **封闭状态集 + 终态/非终态集合**（`TERMINAL_STATUSES` / `NON_TERMINAL_STATUSES`），条件更新传集合，不在各处写状态字符串比较。
2. **Service 是状态唯一写入口**，CRUD 只做条件更新，签名统一 `update_fields_if_status(id, *, allowed_statuses, **fields) -> rowcount`。
3. **CAS 用影响行数判断**，`rows == 0` 即重读再决策；禁止「先查状态再无条件写」。
4. **错误信息分两列** `error_code`（短、封闭、决定可重试性）+ `error_message`（截断 500、仅排障）。

另抄两条实现细节：`QuerySet.update()` **不触发 `auto_now`**，条件更新须显式写 `updated_at`；**终态钩子失败不回滚主状态**（避免副作用失败把已发出的消息标成 failed 进而重发）。

**单飞锁的形状**取自 `app/core/storage_gc.py`：`pg_try_advisory_lock`（try 不阻塞、acquire/unlock 必须 pin 同一连接、非 PG 降级不 pin、持锁占 1 个池连接）。我们用两把：worker 全局单实例 `(常量A, 1)`、每节点发送串行 `(常量B, app_id)`。

**明确不抄的部分**

- **Celery + Redis broker**：否掉「仅 DB 轮询」常见理由是「缺成熟重试/队列/并发控制」，那是在**水平扩容 + 多种 AI job + 多租户**语境下。我们单机、单业务类型、且要求按 `app_id` 严格串行限速——Celery 默认并发会打穿风控。**同一备选方案，场景不同结论相反，不是矛盾。**
- **`JobTypeSpec` + Handler registry 间接层**：为该场景的多队列/多时限设计。P0 只有「好友回复」一种业务，不预留；将来真出现第二种类型再引入，属纯增量。
- **业务挂载机制**（`media_references` bind/unbind、credits freeze/confirm/release、cancel_hooks）：那是它的业务，与我们无关。

**时区按本仓库 Tortoise 1.x 默认走。** `tortoise-orm>=0.25.4` 在 0.25.4 默认 `use_tz=False`（读出常 naive）；本仓库是 1.x（默认 `use_tz=True`）。没配 `use_tz` 在 1.x 不是「读出 naive」，而是「时区开着，写入 naive 会 RuntimeWarning」。

本仓库约定：内部 aware UTC（`utcnow()` = `datetime.now(UTC)`）；`TORTOISE_ORM` 显式 `use_tz=True, timezone="UTC"`；Python 比较用 `as_utc()`（naive→UTC，禁止剥 tz）。**租约/超时仍一律在 SQL 里做**——那是回收纪律，不是因为 naive。日配额「今天」继续 `QUOTA_TZ=Asia/Shanghai`，与 ORM timezone 正交。不要关 `use_tz` 来消警告。

细节见 `architecture.md` §4.3、§5.1–§5.9、§10、§11。

## 0007: LLM 走 openai SDK + 百炼 OpenAI 兼容模式

LLM 调用用 **`openai` Python SDK** 打百炼的 **OpenAI 兼容端点**（`settings.DASHSCOPE_BASE_URL`，形如 `.../compatible-mode/v1`）。不用 `dashscope` 原生 SDK。

### 为什么不用原生 dashscope SDK

**先说结论：`dashscope` 并没有不支持自家模型——是我们最初选错了调用类。** 记下来是为了让后来者不再踩：

DashScope 原生协议按**模型架构**分流端点，而 `dashscope` SDK 的请求 URL 是**按类名推导的、与模型名无关**（`_get_task_group_and_task` 只看模块路径）：

| 端点 | 服务对象 | SDK 类 |
|---|---|---|
| `/api/v1/services/aigc/text-generation/generation` | 纯文本模型 | `Generation` / `AioGeneration` |
| `/api/v1/services/aigc/multimodal-generation/generation` | 多模态模型 | `MultiModalConversation` / `AioMultiModalConversation` |

`qwen3.8-flash` 是**多模态族**模型（实测可接受图片输入），所以用 `Generation`（纯文本端点）调它会得到 `400 url error`。官网对该错误的定义正是「用文本端点调多模态模型」。

**陷阱在于**：选错类不会提示「你用错了类」，只会返回一个看起来像「模型不存在」的 `url error`。实测矩阵：

| 调用方式 | 模型 | 结果 |
|---|---|---|
| `AioGeneration`（text-generation） | `qwen3.8-flash` / `3.7-plus` / `3.8-max` | **400 url error** |
| `AioGeneration`（text-generation） | `qwen-plus` / `qwen-flash` / `qwen-turbo` | 200 |
| `AioMultiModalConversation`（multimodal-generation） | `qwen3.8-flash` | 200（有原生 async） |
| openai SDK / `Completions.create`（compatible-mode） | `qwen3.8-flash` | 200 |

### 选定兼容模式的理由

1. **走官方 Agno DashScope**：`agno.models.dashscope.DashScope` 是 `class DashScope(OpenAILike)`——本质就是 OpenAI 兼容客户端，`enable_thinking` 走 `extra_body`。`.env` 用 `.../compatible-mode/v1`。
2. **符合决策 0002**：当初就写「一次 OpenAI 兼容 `chat/completions`」。
3. **不受模型族影响**：兼容模式一个端点通吃所有文本模型；原生协议下必须让类与模型族配对，换个模型就可能报误导性的 `url error`。
4. **原生 async、公开稳定 API**：`AsyncOpenAI` 是协程，不像 `dashscope` 兼容客户端那样只有同步版、且位于未 re-export 的内部模块。
5. **迁移成本最低**：后续评估 Agno / LangChain 或换厂商，都讲 OpenAI 协议，改 `base_url` 即可。

### Considered Options

- **`dashscope` 原生 `AioMultiModalConversation`**：能跑通且是官方一等接口、有原生 async。未选是因为「类必须与模型族配对」这个耦合已证明是误导性故障源（换模型即可能踩）。
- **`dashscope` 自带兼容客户端 `aigc.chat_completion.Completions.create()`**：既算用 dashscope、又走兼容模式。未选因为它**只有同步版**（FastAPI 里须 `asyncio.to_thread` 包装）且**未在顶层导出**（内部路径，升级易变）。
- **`dashscope` 原生 `Generation`**：不支持 `qwen3.8-flash`，排除。

### 依赖

- `pyproject.toml`：`openai>=1.0`，**不装** `dashscope`。
- 若将来 P2 之后的记忆/向量/语音要走 DashScope 原生能力，再单独加 `dashscope`，与本决策不冲突。

## 0008: 限速拆准入/发放，按 RateDomain 组装；租约加 fencing

v0.1 把四层闸门画在 `postText` 之前的一段 inline 流程里，且在已持有 `sending` 时等待。这会让租约和发放等待撞车（双发），也会让 P2 下载 `if download: 改间隔`。v0.2 收成三条工程边界（怎么做见 `architecture.md` §8、§10）：

1. **准入在生成前，发放在认领前。** 熔断 / 日配额 / outbox 积压不过 → 不认领 inbox，避免先花钱再排队。好友窗口 / 令牌桶 / 间隔在 peek 之后 sleep，CAS 认领后再立刻 `postText`。租约只覆盖 HTTP。
2. **Service 只认 `admit` / `wait_to_dispatch`。** 闸门按 `RateDomain` 注册在 `core/rate_limit/`。P0 只有 `OUTBOUND_TEXT`。下载、加好友、群各自一个 Plan，禁止复用发送桶。
3. **租约必须带 `lease_gen`。** 回收抬代次；标 `sent` 必须 CAS 对上，对不上丢弃 HTTP 结果。没有代次的 `leased_until` 不能同时服务崩溃回收和超时不确定。

一并定下：P0 多 `app_id`（env，不建表）；LLM 失败进终态不重试；组装只取 `delivered=true`；P0 不做养号、不留空配置。

### Considered Options

- **继续把四层写在 outbound 函数里**：P0 最短，P2 一定长成 `if domain`。否决。
- **认领后再 sleep**：发送槽被窗口等待占满，租约先到期，回收再认领，原 HTTP 成功 → 双发。否决。
- **LLM 429 退避重试**：终态被写穿，同一批上下文可能把同一句再生成一遍。否决；崩溃回收退回 `pending` 足够。
- **用 `aiolimiter`**：`max_rate` 即容量，`(40, 60)` 会瞬时 40 条。手写 15 行桶。
- **P0 先单 `GEWE_APP_ID`**：发送循环和限速作用域已经是 `app_id`，单值只是多值的特例，省不了结构。P0 直接多节点。

## 0009: 两套错误码；账号生命周期不进本服务

HTTP 响应用已有的 `AppException` 数字 `code`（`app/core/exceptions.py`：不耦合 `HTTPException`，处理器只转 `{code,message,data}`）。队列表用字符串 `error_code` 决定能不能再试。GeWe 信封是 `{ret, msg, data}`，官方**没有**失败码表；`gewe_client` 把 `ret`/`msg` 译成内部码后丢掉，Service 只看集合。禁止在 outbound 里比对中文。映射与闭集见 `architecture.md` §11.3。

账号上下线（扫码、重连、解封）在 GeWe 控制台。本服务只有 Token 和回调。掉线类 `msgType` 或发送译成 `gewe_offline` / `gewe_risk` → 准入失败 + 告警。不轮询 `checkOnline`，不自动 `reconnection`。

CRUD 条件更新签名以架构为准：`update_fields_if(id, match=..., fields=...) -> rowcount`（0006 抄的是 CAS 纪律，不是函数名）。

### Considered Options

- **把 GeWe `ret` 直接写入队列表**：官方 `ret` 不稳、500 既可能是缺 Token 也可能是网关。否决；必须先译成内部码。
- **P0 轮询 `checkOnline` 当健康检查**：假在线是已知问题，且把账号管理做进管道。否决。
- **Webhook 失败也套 `{code,message,data}`**：官方要空 200。否决。

## 0010: 人/槽拆开；节点从回调学习；熔断并进 nodes

GeWe 官方称 `appId` 每次登录都会变。会话、白名单、配额、发送串行、熔断按本号 `wxid`；`appId` 只当 `postText` 和官方去重的当前句柄。映射表 `nodes`：`id` PK、`self_wxid` / `current_app_id` UNIQUE、熔断两列、`created_at` / `updated_at`。

env 不存 `appId`，也不存 wxid 名单；删 `GEWE_APP_ID` / `GEWE_APP_IDS`。节点只在合格私聊回调 upsert。`GEWE_ALLOWLIST` 空 = 白名单关闭。

熔断不单独建表、不用 bool。换槽覆盖钥匙，不自动关熔断。不为系统事件给陌生号建节点。worker 发送循环从 `nodes` 动态补，启动时 0 条。

### Considered Options

- **继续用 `app_id` 当人**：重登后历史、白名单、配额、熔断全断。否决。
- **env 存 wxid 预建 nodes**：启动时仍没有 `appId`，多一份花名册。否决；回调学习即可。
- **完全不用 `appId`**：`postText` 必填设备 ID。否决。
- **`node_circuit` 独立 + `active` bool**：生命周期不同且 bool 抹掉截止时间。否决；熔断两列挂在 `nodes`。

## 0011: 回调入站凭据是 `Authorization`，不是 `X-GEWE-TOKEN`；控制包免鉴权按形状放行

出站调 GeWe API 用头 `X-GEWE-TOKEN`（值 = `GEWE_TOKEN`），入站回调的凭据则走 **`Authorization`** 头（值 = `WEBHOOK_SECRET`，原文无 `Bearer` 前缀）。两者方向相反、密钥也不同，不能互相替代。

早期实现把头名写成了 `X-GEWE-TOKEN`——那是出站的凭证位，真实回调不带它，于是真消息会被 401。控制台「访问控制 → 鉴权方式 = Header Secret」下发的就是这个 `Authorization`，所以入站密钥必须与控制台配置对齐、与 `GEWE_TOKEN` 分离。空 `WEBHOOK_SECRET` 直接判失败，避免空配置变成「全放开」。

GeWe 自身的两个非业务控制包——验证包 `{testMsg, token}`、订阅确认包 `{msg, callBackUrl}`——**不带**任何凭据。必须放行，否则控制台「一键检测」永远显示失败。放行方式是「按形状命中即 return」：不进 DTO 校验、不挂 `BackgroundTasks`、不触库，因此放行**只是一个空转出口**，不扩大攻击面；真正的防线仍是对业务包的 `Authorization` 校验。

由此带来两个约束：body 必须先于鉴权读取（控制包只看 body 才能识别），且回调路径要设 body 上限防畸形超大请求。

### Considered Options

- **沿用 `X-GEWE-TOKEN` 当入站头**：真实回调不带该头，等于全量 401。否决。
- **用 body 的 `token`（= `GEWE_TOKEN`）当第二把入站密钥**：那是 GeWe 自检包的回显字段，可任意伪造，用它不增加任何保证。否决；凭据只认 `Authorization`。
- **不放行控制包**：控制台检测恒失败，且失败原因难排查。否决。
- **控制包放行前再叠一层业务字段 denylist**：该分支本就出不去（命中即 return），denylist 不承重，且字段名易漏、GeWe 改版会误伤合法包。否决；只做正向形状判断。

## 0012: 部署取同镜像三角色；迁移独立跑一次，worker 锁死副本数

容器做法是两阶段 uv 构建、gunicorn + UvicornWorker、`--timeout` 是 worker 心跳而非请求时长上限。**不要在 entrypoint 里无条件 `aerich upgrade`**：两副本会同时抢 schema。

本项目改为同镜像三个角色，由 `docker-entrypoint.sh` 按 command 分发：`migrate` / `app` / `worker`。

**迁移不进应用启动流程。** app 与 worker 各自启动都跑迁移会并发抢跑，并把「进程启动」与「schema 变更」耦合在一起。拆出一次性 `migrate` 角色，由 compose 的 `depends_on: service_completed_successfully` 保证顺序；K8s 上可平移为 Job + initContainer。

**worker 单实例由代码锁保证，不只靠编排约定。** `architecture.md` §4.3 已论证两个 worker = 两套内存限速桶 = 实际速率翻倍，是封号风险。`app/workers/reply.py` 启动时抢 PG advisory lock，抢不到即 `exit(1)`。

### Considered Options

- **entrypoint 无条件迁移**：最省事，但两副本会同时抢 schema。
- **镜像只给 app，worker 换 entrypoint**：也行，但角色定义会分散在 compose 与 Dockerfile 两处；集中在 entrypoint 更易读。
- **worker 靠「只起一个容器」的编排约定**：Docker / K8s 滚动更新会短暂并存两实例，约定挡不住；必须代码锁。
- **worker 用分布式限速替掉内存桶**：改动限速实现，超出本次范围；P0 用锁即可（`architecture.md` §4.3 已注明单实例是部署选择、不是 API 形状）。

## 0013: 生成层改用 Agno Agent；只用 Agent，不引入 AgentOS

0013 取代 0002 关于「生成实现」的那一半。生成由 `openai` SDK 直调百炼，改为经 **Agno `Agent`** 产出文本；`services` 的调用方式不变。同期落地「每个执行节点一个专属 Agent」的骨架（一期只有占位 Agent `reply`）。

**为什么改**：一是要为每个微信号配专属人设（`architecture.md` §9 早已写「P1 做按执行节点 persona」），Agent 的 `instructions` 比裸 `chat/completions` 更适合承载人设与边界纪律；二是图片要进多模态输入，官方 `DashScope` 适配器与 `Image(url=...)` 省掉了自写多模态序列化。

**边界没变，反而被显式钉住**：生成仍是「只吃数据、只吐文本」，且新增一层薄接缝——`services` 只认识 `app/utils/generation.py`，**禁止直接 `import app.agno`**。换框架时理想情况只改接缝那一个文件（§14.4）。这条纪律写进 `AGENTS.md`。

**为什么不用 AgentOS**：AgentOS 会挂 HTTP 路由、JWT 中间件与会话表，等于把「一期无网页端」和「会话历史归 `chat_sessions`」两条红线一起踩掉。只取 Agent 生成能力，其余全部丢弃。

**为什么不用它的 session/memory**：`chat_sessions` 是历史唯一事实源。故 `db=None`、`add_history_to_context=False`，历史由编排层组装成 `list[Message]` 传入；并且**绝不挂 `learning`**——它会把 `add_history_to_context` 自动置 True，等于让框架接管历史。每轮还必须显式传 `session_id`：不传时 agno 会生成 UUID 并**回写 Agent 实例**（sticky），而实例是按 agent_id 缓存、worker 又长驻，会跨会话污染。

**三个实测确认的关键事实**（决定了实现细节，改动前请先复核）：
1. 非流式 `arun` **不抛异常**：模型报错时返回 `status=RunStatus.error` 且 `content=str(e)`。接缝**必须显式查 `status`**，否则会把「Connection error.」这类错误文本当回复发出去。已用坏端点实测复现。
2. `Agent` **不接收** `temperature` / `max_tokens` / `timeout` —— 它们只是 model 层字段，传给 Agent 会 `TypeError`。
3. `telemetry` 默认 `True` 会往 `os-api.agno.com` 上报；`Agent(telemetry=False)` 可关（已实测 0 次调用）。

**版本取 3.0.9 而非 2.8.7**：2.x 已停维护；对「不落库、纯生成」的用法，3.0 的破坏性变更集中在 session/db 与 memory 参数改名，正好不涉及我们（`DashScope` 适配器两版逐字节相同、`arun` 返回类型未变）。3.0 还把遥测改成 fire-and-forget，去掉了 2.8.7 遥测在关键路径同步等待的延迟风险。

### Considered Options

- **只换 model（`agno.models.DashScope`），不上 Agent**：改动更小，但答非所问——需求是 Agent 生成能力与人设承载。
- **上 AgentOS**：会引入路由/JWT/session 表与 `learning`，同时撞三条既有红线。只保留 channels → models 工厂 → agents 的结构，落点 `app/agno/`。
- **也照抄 `BailianDashScope` 子类**：它的自定义逻辑是**视频**序列化，本项目用不到；`base_url` 靠工厂收口即可，不需要子类。
- **保留 `LLM_MODEL` 等 env 旋钮**：会产生「代码目录」与「环境变量」两个事实源。模型 id 进渠道目录、生成参数作常量。
- **抢救 `status_code` 做细分错误码**：一期不值——`llm_*` 在 `INBOX_NO_RETRY` 里全是终态不重试，细分只影响排障精度；真需要时再加十行薄子类。

## 0014: 长回复拆条投递；媒体静默窗

一次生成可能超过微信单条长度，图文也可能被 debounce 窗口拆成两轮。拆条和静默窗都落在投递层，不改发送限速与表结构。

**`MODEL_MAX_TOKENS` 取 4096**：这是上限不是目标值，对短回复无影响。调小会把长回复**静默截断**且 `status` 仍是 `completed`。

### 1. 长回复拆条：生成侧拆，投递层不改

**问题**：微信单条文本有长度上限，超长会被截断或静默丢弃。

**做法**：纯函数 `app/utils/text_split.py` 按 **UTF-8 字节**（不是字数——汉字 3 字节，差 3 倍）切成 N 段，`services/reply.py` 在同一事务里循环 `create_pending` 写 N 行 outbox。发送层、限速域、DB 结构**全部不动**：
- `outbox` 没有 `(self_wxid, friend_wxid)` 唯一约束，多条 pending 合法（已有测试就在造多行）。
- `peek_next` 是 `ORDER BY o.id`，同事务插入的 N 行 id 连续 → **段落顺序自动正确**。
- 每段各自走原有的限速/重试/CAS 语义，无需新的协调。

**关键：历史存完整文本**。`append_turn` 写的仍是未拆分的完整回复——这保住了「报告文本即图片的文本编码」，是**不做图片回挂**的前提（7 天后追问细节，模型读自己的报告仍答得出）。

**已知后果（接受）**：`mark_last_assistant_delivered` 在每条 sent 后执行，故第 1 段发出就整轮标 `delivered=true`。改成「末段才标」更糟：中间段永久失败会让整轮被 `delivered_window` 过滤掉，历史里连前几段都不剩，还可能触发重复回复。

**代价**：N 段 = N 次 `postText`，每段吃满打字延迟上限 5s，日配额与 backlog 按 N 倍消耗。

**待校准**：GeWe 官方 `postText` 文档对 `content` **未给任何长度约束**，超限是微信协议层行为且行为未知（报错 or 静默丢）。`WECHAT_TEXT_MAX_BYTES=2000` 是保守值，需真机实测校准。

**不做 Markdown 归一化**：决定原样发出，客户会看到字面的 `##`/`**`/`![]()`。

### 2. 媒体静默窗：不是调大 DEBOUNCE_MS

**问题**：`DEBOUNCE_MS=2000` 的窗口右界锚在「首条消息到达时刻」，而媒体下载要串行 3~10s/张。于是「发图后紧接着打字提问」的问题会落在窗口之外，被拆成两轮——**第二轮没有图**。

**为什么不能只调大 `DEBOUNCE_MS`**：它是**文本窗**，调大会让每条纯文字回复都背这个延迟；且锚点错——媒体场景下有用的锚点是「最后一个媒体解析完成的时刻」，不是首条到达时刻。

**做法**：媒体会话的窗口右界改为 `max(文本窗, MAX(media.updated_at) + MEDIA_QUIET_MS)`，defer 门同步扩展为「还有 pending **或** 刚解析完不足静默窗」。两者共用同一表达式 → 认领恰好发生在窗口刚过期时，等待期间到达的消息必然全部收进同一批。

**`GREATEST` 忽略 NULL**：无媒体的会话第二个子查询为 NULL，右界自动回落文本窗 → **纯文本行为逐字不变**（有回归测试守着）。

**`MEDIA_QUIET_MS=3000` 不需要开大**：媒体下载期间 defer 一直按着，本身就是免费的等待窗，静默窗只需覆盖「解析完成之后才打进来的字」。开大到 LLM 量级会把「已看到回复之后」发的消息也吞进上一轮。

**媒体窗只在 `DEBOUNCE_MS>0` 时启用**：`debounce=0` 的语义是「不设窗口」，此时再加媒体窗反而把 `MAX(updated_at)` 这个**静态**时刻当上限，晚于它的消息会被永久排除、会话卡死。

### 3. `fail_stale` 双谓词：区分「排队」与「卡死」

原实现按 `created_at + 30s` 绝对年龄判死，而串行下载 3~10s/张、积压上限 30 张需约 200s → **第 6 张起就在排队中被判死**，有效容量被压到约 5 张。多图场景下还会因 `pending` 提前清零，让会话在图没取全时就回复。

改用 `attempt` 区分：`attempt > 0`（已发起下载未收终态）按 `MEDIA_ATTEMPT_TIMEOUT_SECONDS=30` 判卡死；`attempt = 0`（排队中）按 `MEDIA_QUEUE_TIMEOUT_SECONDS=420` 判排队上限。保留后者是为兜底「下载循环挂掉导致永远拿不到 `attempt`」——那会永久 defer 会话。

顺带把 `mark_ready`/`mark_failed`/`mark_skipped` 的时间戳从 Python `utcnow()` 改为 DB `NOW()`：静默窗与超时判定都走 SQL 时钟，混钟会让差值直接变成窗口误差。

### Considered Options

- **改用「生成时让模型自己分段」（prompt 层）**：模型未必遵守，且长度靠模型自估不可靠。否决；改为确定性纯函数。
- **发送时现拆**：崩溃重试会重复发出已发过的段落。否决；落库即拆好，每条独立重试。
- **加 `part_index`/`part_total` 列**：无消费者——顺序由 `id` 保证，delivered 语义选择不改。否决；不引入无读者字段。
- **末段才标 delivered**：失败时整轮被过滤，历史丢失且可能重复回复。否决。
- **只调大 `DEBOUNCE_MS`**：惩罚所有文本回复，且锚点错（跨轮追问结构上覆盖不到）。否决。
- **给媒体会话单独设一个静默窗列（`defer_until`）**：需要迁移与额外写入，而 `media.updated_at` 已能表达「解析完成时刻」。否决。
- **回挂历史图片（`chat_sessions.media_ids`）**：回挂会让模型重看手机照片、可能给出与首轮不一致的结论，且 URL 只活 7 天。否决；真需要时再加（短窗 + 小预算）。
