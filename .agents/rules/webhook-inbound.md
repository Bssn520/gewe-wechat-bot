# Webhook 入站

落点：`app/api/v1/webhook.py` → `app/services/webhook.py` → `app/crud/node.py` / `inbox.py`。
详情见 `docs/architecture.md` §6、§11。

## 可核对事实

- 路径：`POST /gewe/callback`；鉴权失败 1003 / 401
- **入站凭据只有一个：`Authorization` 头**（密钥原文 = `WEBHOOK_SECRET`，**无** `Bearer` 前缀；后端容错前缀）。空配置不放行。**不是** `X-GEWE-TOKEN`——那是出站调 GeWe API 的头；body 里的 `token`（= `GEWE_TOKEN`）也**不是**入站凭据
- **控制包不带凭据，按形状 ACK 空 200、不落库**（判定内联在 `app/api/v1/webhook.py` 路由里）：验证包 `{testMsg, token}`、订阅确认包 `{msg:"设置订阅成功!!", callBackUrl}`。两者都不带 `Authorization`，401 掉会让控制台「一键检测」一直显示失败。放行**只是一个空转出口**——命中即 return，不进 `model_validate`、不挂后台任务，所以放行不等于接受内容；真正的防线是那条 `Authorization` 校验
- ⚠️ **`Authorization` 是否下发由 GeWe 控制台的「Header Secret」决定，`setCallback` API 设不了**（实测 `authType`/`authSecret`/`headerName` 等参数全被忽略）。所以**改回调地址请用控制台**：用 API 单独改 URL 会丢掉鉴权配置，消息被 401
- Router：**鉴权 + 读 body + 解析**后立刻 **空 body / HTTP 200**（俗称走 ping）；`handle_callback` 走 `BackgroundTasks`（同 loop 协程，响应发出后再 await）。禁止在该栈 `await` LLM 或 `post_text`
- 空 200 只表示收到，不表示已进 inbox。后台失败打致命日志，接受极少数丢失（GeWe 超时不补投）
- Service：解析回调 DTO → 丢 `is_self` / 系统事件 / `gh_` / 群 / 非文本 → 合格私聊先 upsert `nodes` → 白名单 → `crud` 插入 inbox `pending`
- 去重靠 inbox 唯一约束 `(self_wxid, new_msg_id)` 与 `(app_id, new_msg_id)`（**字符串**，可能 > 2^53），不靠应用层先查后插
- 缺 `new_msg_id`：丢弃并打日志，不入 inbox
- 重复键：第二次及以后不插入、不报错给 GeWe（仍 200）
- **只处理文本**：`msgType` 必须是 `TEXT`。P0 只做纯文本，**其余 `msgType` 一律不落库**（含 `IMAGE`/`VOICE`/`VIDEO`/`EMOJI`/`FILE`/`LINK`/`MINI_PROGRAM`/`TRANSFER`/`RED_PACKET` 等）
- **群判定**（v2 扁平）：`toUser`/`fromUser` 含 `@chatroom`，**或** `content` 以 `fromUser + ":\\n"` 开头 → 群消息，丢弃。不要只看 `@chatroom`——v2 群消息常把发言人放在 `fromUser`、群标识不在顶层，正文才带 `wxid:\\n` 前缀；漏判会当成私聊回复该好友
- **验证请求**：配置回调后 GeWe 会发一条 `{"testMsg": ..., "token": ...}` 的自检包（无凭据，Router 直接 ACK）；也有 content=「验证回调地址是否可用」的消息形态，走 Service 按 `VERIFY_CONTENT` 忽略。两者都必须 200 且不入 inbox
- **系统事件**（官方 v2，**无** `Offline`）：`LOGOUT` / `LOGIN_ERROR` / `LOGIN_SUCCESS` / `RECONNECT_SUCCESS` / `RECONNECT_FAIL` / `LONG_SUCCESS` / `LONG_FAIL` / `Long_Serve_Start_Success` / `Long_Serve_Close` / `SYSTEM` → 不落库、零 outbox。掉线类命中已有 `nodes` 行才熔断。上线类仅更新已有行的槽并 `close_if_expired`。不为陌生号建节点。**不**自动重连
- 群、`is_self`、验证包、系统事件**不落库**，打日志事件 `ignored`（不是 inbox 状态），零 outbox
- 写库失败：插入仍设短超时，失败打致命日志；ACK 已发出，不再用写库时间赌 3 秒窗口

> 回调字段与 39 种 `msgType` 全表见官方「[回调结构说明 v2.0](https://doc.geweapi.com/doc-8680561)」；多模态接收链路与限速见 `docs/platform-limits.md` §6。**官方明确 `newMsgId` 才是唯一 ID**（`msgId` 为旧 ID，可能为 0）。

自有接口（健康检查等）用 `{code,message,data}`；**webhook 成功响应必须是空 body**，不套这层壳。
