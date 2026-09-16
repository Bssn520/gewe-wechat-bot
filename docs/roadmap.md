# 分期与路线图

> 只记**分期、目标、边界**。P0 的落地细节在 [`architecture.md`](./architecture.md)（唯一详情出处），决策理由在 [`decisions.md`](./decisions.md)。
> 本文仅供人读方向；实施以 `AGENTS.md` 红线为准。

| 阶段 | 内容 | 状态 |
|------|------|------|
| **P0** | 好友文本自动回复：Python 服务、私聊白名单、inbox/outbox、百炼文本 | 📐 待实施 |
| P1 | 节点表 / 熔断落表 / 进程保活（登录仍在 GeWe 后台） | ⏳ 未开始 |
| P2 | 多媒体：图片 / 语音收发 | ⏳ 未开始 |
| P3 | 群聊：默认 @ 才回 | ⏳ 未开始 |
| P4 | Agent 循环：工具 / MCP / skills | ⏳ 未开始 |

P2–P4 **实施前必须各自再开调研**（官方产品线、能力边界、风控约束都会变）。

---

## P0 好友文本自动回复

**目标**：长期运行、无 UI 的服务。GeWe 回调接入多个执行节点，对**白名单个人好友**做纯文本自动回复。模型百炼 `qwen3.8-flash`（thinking 沿用默认，不显式关闭）。

**非目标**：网页/管理后台；群自动回复；图片/语音/视频；Celery / Redis broker；Agno / LangChain / Pi / AgentOS；长消息拆分；另建项目/仓库当入口（worker 进程仍在本项目内 `app/workers/reply.py`）。

**范围**

1. FastAPI 骨架：`api` / `services` / `crud` / `models` / `schemas` / `utils` / `workers` / `core`
2. GeWe 薄客户端：`post_text`（自写 httpx，**官方不提供 SDK**）
3. `POST /gewe/callback`：鉴权、3 秒空 200、去重、丢群、白名单、写 inbox
4. Worker：两个循环——生成（准入 → inbox → LLM → 写 `chat_sessions` + outbox）与发送（peek → 发放等待 → CAS 认领 → `post_text`）。P0 多 `app_id`
5. 会话库内存 200 轮、给模型 20 轮，只取 `delivered=true`；模型与 Key 走 settings
6. 健康检查；`.env.example`；测试覆盖去重 / 群丢弃 / webhook 不调 LLM / 未送达不入上下文

**验收**

| 项 | 通过标准 |
|----|----------|
| 发送 | `post_text` 打 filehelper 或白名单小号成功 |
| ACK | 手机发一条，回调 3 秒内 200，随后异步出现回复 |
| 去重 | 同一 `new_msg_id` 只入 inbox 一次、只回一次 |
| 群 | 进回调、不落库、日志事件 ignored、零 outbox |
| 非白名单 | 不回 |
| 思考 | 不传 `enable_thinking`，沿用百炼默认（开启） |
| 分层 | Router 无 ORM / 无外呼 |
| 限速 | 不同好友 ≤10 位/分钟；发放先等再认领（见 `architecture.md` §8） |

落地顺序（依赖递进）：**先通发送 → 再通「收→回」→ 再会话记忆 → 最后才框架 / 多媒体**。

---

## P1 保活与账号表

**目标**：P0 已按多 `self_wxid` 发送，节点从回调学习。P1 可选：白名单从 env 迁到表；API 与 worker 同镜像分进程；systemd / docker 保活。**登录/重连/解封仍在 GeWe 控制台**，本服务不代劳。

**非目标**：Celery（仍按决策 0003 的切换条件评估）、群聊、多媒体、AgentOS。

**要点**

- **一 Token 一回调 URL**；隔离不够就多 Token（官方 FAQ）。到期随机下线是采购/控制台的事。
- 出站按 `self_wxid` 串行，禁止多 worker 抢同一节点（`platform-limits.md` §8）。
- **不**轮询 `checkOnline`，**不**自动重连。掉线靠回调事件 + 发送错误码。
- 独立 worker：`make worker`，与 API 共用同一数据库。
- 账号维度**完全隔离**：独立设备/IP、独立 appId/配置；见 `platform-limits.md` §3、§4（24h 充电、位置不动、同 WiFi 批量养号都会封）。

---

## P2 多媒体

**目标**：文本闭环稳定后，收发图片 / 语音（后续视频）。Schema 预留 `{type, text, media?}`，**不要把消息写成唯一的 `content: str`**。

**非目标**：朋友圈、视频号；重写一期纯文本范围。

**约束**（官方口径，**详见 [`platform-limits.md`](./platform-limits.md) §6**）

- **接收**：回调只给 XML，须另调 `downloadImage` / `downloadVoice` / `downloadVideo` 换 `fileUrl`；**`fileUrl` 仅 7 天有效**，须尽快落盘。
- **下载是独立限速域**：必须串行队列 + 每条 3–10 秒间隔，**不能复用发送的令牌桶**；频率过高会掉线。
- 图片要试不同质量（高清/常规）；语音是 silk，需转码；文件「需使用第二条回调下载」。
- **出站**：媒体通常要公网 URL，连发图片有失败样本。落点在 `utils/gewe_client.py` 与 inbox Payload，**不改 Router 的 3 秒 ACK 语义**。

---

## P3 群聊自动回复

**目标**：在白名单群内，**默认 @ 才回**。一期代码路径保持「见群即丢」。

**非目标**：群内逢消息必回；群管理（拉人/踢人）；欢迎语广播。

**约束**

- 个微风控对群高频同质化敏感（`platform-limits.md` §2 静默降权）。
- **不要把 P0 的 outbound 默认改成群可用**；单独开关与限速。
- 群消息识别：`fromUser`/`toUser` 含 `@chatroom`，或 `content` 以 `fromUser + ":\n"` 开头（v2 常见：发言人在 `fromUser`，顶层没有群 ID）。

---

## P4 Agent 循环（工具 / MCP / skills）

**目标**：当回复策略需要工具调用、MCP 或 skills 时，**只替换「生成」那一步**。Ingress、去重、白名单、outbox 不动（边界见 `architecture.md` §14）。

**非目标**：把 AgentOS / Pi / LangGraph 当 GeWe webhook 入口；在 callback 里 `await agent.arun()`。

**候选**：**Agno `Agent` 已落地**（决策 0013，`app/agno/` + `app/utils/generation.py` 接缝），但**只取 Agent 生成能力**：无 db、无 history、无 tools、无 AgentOS。本阶段就是在既有 `Instruction`/人设之上挂 tools / MCP / skills——加在人设模块与注册表，不改 ingress/outbox。LangGraph / Pi 默认不选。

**注意**：若届时生成会调**真实副作用工具**，则决策 0005 里「生成重试是安全的」前提不再成立，需引入幂等键（`architecture.md` §14.5）。
