# 执行节点与白名单

落点：`app/models/node.py`、`app/crud/node.py`、`app/crud/node_circuit.py`（熔断打 `nodes` 两列）、`app/core/config.py`。
详情见 `docs/architecture.md` §5.4。

## 可核对事实

- 节点从合格私聊回调学习，**无** `GEWE_APP_IDS` / `GEWE_APP_ID`
- `nodes`：`id` PK；`self_wxid` / `current_app_id` UNIQUE NOT NULL；`circuit_open_until` / `circuit_reason` 可空；`created_at` / `updated_at`
- 同一 `self_wxid` 换槽 → 覆盖 `current_app_id`，告警，**不**自动关熔断。槽已被别人占用 → 拒绝改绑
- `GEWE_ALLOWLIST` 空 = 白名单关闭（过群 / `gh_` / `isSelf` 后都回）。非空 = `本号wxid:好友|好友,...`；某个本号没出现 = 该号不回
- 白名单语义 `(self_wxid, friend_wxid)`；不在名单的私聊 ACK 后可仍更新已认号的钥匙，但不落 inbox
- 群：`toUser`/`fromUser` 含 `@chatroom`，或 `content` 以 `fromUser + ":\\n"` 开头 → **不落库**，日志事件 `ignored`，不得发消息
- Token / 回调 URL：一 Token 一回调；多账号可共享 Token
- **同一 `self_wxid` 同时只允许一条 outbound `sending`**；每节点一个 sender task，由监督循环从 `nodes` 补齐。**不同号之间并行**
- **worker 单实例**：启动抢 advisory lock；多实例会让每节点内存桶翻倍。API 可水平扩
- 间隔 / 好友窗口 / 日配额 **查 outbox `sent_at`**，按 `self_wxid`
- **不做养号**、**不做账号生命周期**。扫码/重连/解封在 GeWe 控制台
- 熔断截止落在 `nodes`，重启不得解除。到期才清
- **不**轮询 `checkOnline`，**不**自动 `reconnection`
