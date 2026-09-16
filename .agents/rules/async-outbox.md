# Inbox / Outbox 与 Worker

落点：inbox / outbox / chat_sessions / nodes Model，`services/reply.py`、`services/outbound.py`、`workers/reply.py`、`app/core/locks.py`、`app/core/rate_limit/`、`app/core/gewe_errors.py`。
详情见 `docs/architecture.md` §4–§11（架构决策见 ADR 0005、ADR 0006、ADR 0008）。

## 可核对事实

- inbox 状态：`pending | processing | done | failed`（**无 `ignored`**，群/验证包不落库）；outbox：`pending | sending | sent | failed`
- 仅 Service 写状态；CRUD 只做条件更新
- **状态迁移一律用 CAS**：`update_fields_if(id, match=..., fields=...) -> rowcount`；发送成功 `match` 必须含 `lease_gen`。`rows == 0` 即重读再决策。终态集合：inbox `{done, failed}`，outbox `{sent, failed}`
- **`QuerySet.update()` 不触发 `auto_now`**，条件更新必须显式写 `updated_at`
- **错误分两列**：`error_code`（短、封闭）+ `error_message`（`[:500]`）。比对内部码，不比 GeWe 中文。闭集与 `ret`/`msg` 映射见 `docs/architecture.md` §11.3，落点 `core/gewe_errors.py`
- **终态钩子失败不回滚主状态**：回填 `delivered` 异常只记日志，**不得**把已发出的消息标成 `failed`
- **两个循环，并发语义相反**：
  - 生成循环：先 `plan.admit`（熔断 / 日配额 / 积压），再按会话批量认领；可并发不同会话，每 `self_wxid` 封顶 + 全局闸门。**准入失败跳过该 `self_wxid`，继续 peek 下一个号**，不要让整个生成循环去睡。LLM 失败 → inbox `failed` **终态，不重试**
  - 发送循环：每 `self_wxid` 一个 task（监督循环从 `nodes` 补）；**peek → `circuit_ok` → `wait_to_dispatch` 睡完 → 再 `circuit_ok` → CAS 认领（写入 `app_id`）→ 立刻 `postText`**。熔断开着禁止 `postText`。禁止占着 `sending` 睡发放等待
- `processing` / `sending` 必须带 `leased_until` **和** `lease_gen`。回收器退回 `pending` 时 **+1 代次**。标 `sent` 必须 CAS 对上代次，对不上丢弃 HTTP 结果、禁止再发
- outbox 租约只覆盖 HTTP，不含发放等待
- **内部时刻 aware UTC**：`utcnow()` = `datetime.now(UTC)`，禁止 `.replace(tzinfo=None)`；比较用 `as_utc()`（naive 当 UTC）。`TORTOISE_ORM` 显式 `use_tz=True, timezone="UTC"`，不要关 `use_tz` 消 warning
- **租约/超时比较只在 SQL 里做**（`NOW()` vs `timestamptz`）；熔断/间隔等 Python 比较必须双方 aware，不要剥 tz。日配额「今天」用 `QUOTA_TZ`（上海），不要用 UTC 日切
- `services/reply`：准入 → 认领 → 组装（**只 `delivered=true`**）→ 生成（只吃数据、只吐文本）→ 同一事务写历史（assistant `delivered=false`）+ outbox + inbox `done`
- `services/outbound` **不实现闸门**。只调 `registry.for_account(self_wxid, OUTBOUND_TEXT).wait_to_dispatch(intent)`。闸门在 `core/rate_limit/`，按 `RateDomain` 组装。P0 只有 `OUTBOUND_TEXT`；下载 / 加好友 / 群禁止复用该 Plan
- 发放三层：好友窗口（去重**集合**，已在窗口内立刻放行；新好友且集合已满 → **等到最早过期的那位滑出**，禁止固定睡 5s 就发）+ 手写令牌桶（≤40/分钟、容量≤2，不用 `aiolimiter`）+ 间隔（同好友 1.5–3.5s，不同 4–8s）
- ⚠️ **`postText` 成功 ≠ 送达**。`sent` 只是接口成功
- **串行作用域 = 按 `self_wxid`**：`NOT EXISTS` 必须带 `x.self_wxid = o.self_wxid`；`senders: dict[self_wxid, Task]`
- **每节点一条 `sending`**：部分唯一索引 `(self_wxid) WHERE status='sending'`
- **worker 单实例**：启动抢 `pg_try_advisory_lock(常量A, 1)`；API 可水平扩，worker 不行
- **单飞锁四约定**（`app/core/locks.py`）：try 不阻塞；pin 同一连接；非 PG 不 pin；持锁占 1 池连接
- P0 **不做养号**，不留 `WARMUP_*`
- 发送：`gewe_timeout` / `gewe_unavailable` 只再试一次；`gewe_offline` / `gewe_risk` → 节点熔断；`gewe_auth` → 全局告警。不自动重连、不轮询 `checkOnline`
- 一期 **无 Celery、无 Redis broker**
- 回收器支持 `--dry-run`、先抢全局锁再扫

## 何时才上 Celery

同时满足再开调研：多机 worker、或多种 job（媒体下载/定时）、或必须把推理从 API 进程拆走。任务体只含 `inbox_id` / `outbox_id`。
