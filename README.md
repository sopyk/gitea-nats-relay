# Webhook2NATS

一个轻量的 **Webhook → Core NATS** 实时门铃转发器。

它接收来源平台（目前是 Gitea / Forgejo）的 webhook，校验签名、去重、解析出"这次事件该通知谁"，然后把一个**精简事件**发布到 NATS，让订阅的 Agent 在毫秒级被叫醒——而不是靠轮询定时抓。

- **运行环境**：Python 3.12 + [`nats-py`](https://github.com/nats-io/nats.py)，打成很小的 Alpine 镜像。
- **传输**：纯 **Core NATS**（即时、at-most-once、纯内存）。**没有 JetStream、不存消息历史、没有数据库**。
- **定位**：只做通知。它**不存正文、不标已读、不代替任何 Agent 行动**。Gitea 始终是事实源；Agent 被叫醒后自己去 Gitea 拉完整内容。

## 为什么要做

一队 Agent 每几分钟轮询一次自建的 Gitea：空请求很多，延迟最多一个轮询周期。这个转发器插在 Gitea 和 NATS 中间——Gitea 每个事件推一次 webhook，转发器校验、压缩、解析收件人，然后往 NATS 发一个"门铃"。在线的 Agent 毫秒级收到；离线的 Agent 本来就收不到（这是 Core NATS 的设计），由另一条**低频 Gitea 巡厅**兜底对账（对账依据是 Gitea 未读状态）。

## 架构

```
┌──────────────┐  webhook（HTTP POST，HMAC-SHA256）   ┌────────────────────┐  Core NATS   ┌──────────────────┐
│    Gitea     │ ───────────────────────────────────▶│   Webhook2NATS     │ ───────────▶ │ 订阅的 Agent     │
│ （事实源）    │  issues / issue_comment /            │  （本服务）          │  门铃 subject │ （在线时收到）    │
│              │  pull_request / pull_request_review  │                    │              └──────────────────┘
└──────────────┘                                      └────────────────────┘
        ▲                                                      │
        │ Agent 自己去拉完整 issue/PR 内容                     │ 去重状态（本地文件）
        └──────────────────────────────────────────────────────┘
```

每个事件的处理流程：

1. **Gitea** 发出 webhook；
2. **Webhook2NATS** 校验 HMAC 签名、按 delivery UUID 去重、依据**外部挂载的成员映射**解析收件人（@提及 / 指派 / 请求评审），然后发布一个精简 JSON 事件（控制在 1 KB 以内）；
3. **NATS** 扇出给匹配的在线订阅者。

如果没有订阅者在线，Core NATS 按设计丢弃——低频巡厅是保证不漏的兜底。**这个服务故意不做持久化存储。**

## 关于"公共转发"：平台适配是可插拔的

它不叫 `gitea-nats-relay`，是因为它**不是 Gitea 专属服务**。真正和平台绑定的只有一小块：

> **验签算法 / 事件头字段名 / 原始 payload 的归一化**

其余全部是平台无关的通用能力：HTTP 接收、delivery 幂等去重、按成员映射路由、发布到 NATS 并等待确认。

**要支持新平台（GitHub / GitLab / Jenkins / …）**：照着现有 Gitea 适配块加一个 provider，实现这四件事即可，核心逻辑不用动：

| provider 要实现 | 说明 |
|---|---|
| 事件类型 header | 事件类型写在哪个头字段 |
| delivery header | 投递唯一 ID 在哪个头字段 |
| 验签算法 | 该平台用什么算法、签名放在哪个头 |
| payload 归一化 | 把平台原始 JSON 转成统一事件字段 |

目前内置的是 `gitea`。这套接口是稳定的——这是本服务设计成通用转发器的关键。

## Subject 契约

转发器在 NATS 上的账号是服务端 ACL 限权的，**只能**发布这三类：

| Subject | 含义 |
|---|---|
| `<NOTIFY_SUBJECT_PREFIX>.<host>.<id>` | 定向通知某个 Agent（默认前缀 `evt.gitea.notify`） |
| `<BROADCAST_SUBJECT>` | 全厅公告（默认 `evt.gitea.broadcast`）——仅配置作者新建的、带公告标签/标题的 issue |
| `hb.<RELAY_ID>` | 服务心跳（仅当配置了 `RELAY_ID`） |

身份（`<host>.<id>`）编在 subject 里，每个 Agent 的服务端权限模板只锁自己的收件位——**伪造身份会在 NATS 鉴权层直接被拒**，不依赖应用层自觉。

## 消息格式

只带元数据，Agent 自己从 `url` 拉完整内容：

```json
{
  "kind": "issue_comment",
  "action": "created",
  "repo": "org/repo",
  "actor": "发送者登录名",
  "ts": "2026-10-10T09:30:00Z",
  "number": 134,
  "title": "…",
  "url": "https://<gitea>/<org>/<repo>/issues/134",
  "state": "open",
  "assignees": ["登录名"],
  "labels": ["…"],
  "comment_url": "https://<gitea>/…/issues/134#issuecomment-42",
  "mentions": ["登录名"],
  "delivery": "gitea-delivery-uuid"
}
```

## 配置 —— 所有环境相关值全部外移

镜像里**只有代码和依赖**，不含任何 IP、主机、域名、账号、口令，也不含成员身份。成员映射是只读挂载的文件，密钥走环境变量文件。

| 变量 | 必填 | 说明 |
|---|---|---|
| `NATS_SERVERS` | 是 | NATS 地址，逗号分隔多个，如 `nats://<nats-host>:4222` |
| `NATS_USER` / `NATS_PASSWORD` | 是 | 转发器专用 NATS 账号（ACL 已限权到门铃 subject） |
| `WEBHOOK_SECRET` | 是 | HMAC-SHA256 密钥，与 Gitea webhook 配置里同一个 |
| `MEMBERS_FILE` | 否 | 登录名→`{host,id}` 映射 JSON，默认 `/config/members.json`（只读挂载） |
| `DEDUP_FILE` | 否 | delivery 去重状态，默认 `/data/seen.json`（可写卷） |
| `RELAY_ID` | 否 | 心跳后缀（`hb.<RELAY_ID>`）；不填则不发心跳 |
| `RELAY_CLIENT_NAME` | 否 | NATS 连接显示名，默认 `webhook2nats` |
| `NATS_HEARTBEAT_SECONDS` | 否 | 心跳间隔，默认 `60` |
| `LISTEN` | 否 | HTTP 监听地址，默认 `0.0.0.0:8090` |
| `PUBLISH_TIMEOUT_SECONDS` | 否 | 等待 NATS 发布确认，默认 `3` |
| `BROADCAST_AUTHORS` | 是* | 逗号分隔登录名，这些作者的新 issue 带公告标签/标题时走全厅 |
| `NOTIFY_SUBJECT_PREFIX` | 否 | 默认 `evt.gitea.notify` |
| `BROADCAST_SUBJECT` | 否 | 默认 `evt.gitea.broadcast` |

\* 代码里**没有内置任何默认作者**；不配就等于不发全厅公告。

`members.json` 示例（挂载在 `/config/members.json`，**不在镜像里**）：

```json
{
  "members": {
    "alice": { "host": "h1", "id": "alice" },
    "bob":   { "host": "h2", "id": "bob" }
  }
}
```

## HTTP 契约

- `GET /healthz` → NATS 连通返回 `200 ok`，否则 `503 nats-down`
- `POST /gitea-hook`（Gitea webhook 配置的回调地址）：
  - `401 bad-signature` — HMAC 不匹配或没配 `WEBHOOK_SECRET`
  - `200 duplicate` — 该 delivery 已成功处理过
  - `409 inflight` — 同一 delivery 正在被一个重叠的重试处理
  - `400 bad-json` — 请求体不是合法 JSON
  - `502 publish-failed` / `504 publish-timeout` / `500 publish-error` — NATS 未确认发布；delivery **回滚**，让 Gitea 重试时复用同一 UUID，而不是被当重复丢弃
  - `200 accepted` — 所有相关 subject 都已发布并 flush 完成；**只有这时**才把 delivery 标记为已处理

### 去重与重试语义

delivery UUID 先进入 `inflight` 状态，**只有在所有 NATS 发布 + flush 都成功后**才落盘为已处理。失败或超时就回滚，让来源平台安全地用同一个 `X-Gitea-Delivery` 重试；重叠重试同 UUID 返回 `409`。状态是 LRU（默认 2000 条）并原子落盘，重启不会重复通知。

### @提及解析

抽取 `@登录名` 时**先剥离代码块和行内代码**——所以反引号或代码块里的 `@bob` 不会触发通知。只有出现在成员映射里的登录名才会生成定向 subject；不在映射里的名字不路由，但会保留在事件的 `mentions` 里供展示。

## 安全说明

- 转发器**不持有 Gitea token**，用的 NATS 账号也只能发布三个 subject、不能读消息、不能冒充任何 Agent。即使被打穿，影响面仅限于伪造门铃——而门铃里只有公开元数据和 URL，没有正文。
- webhook 用 HMAC-SHA256 校验；监听端应放在内网 / tailnet，不暴露公网。
- 密钥在运维方的 env 文件里（`0600`），成员映射是只读挂载。两者都不进镜像、也不进这个仓库。

## 测试

不需要真实的 NATS 或网络——测试里用假的 NATS 客户端、直接调 HTTP handler：

```bash
cd relay
python3 test_relay.py
```

覆盖：HMAC 拒绝、代码块内 @ 不误触、成员映射路由、公告判定、delivery 去重往返、失败重试、inflight 拒绝、端到端 webhook 处理。

## 目录结构

```
relay/
  server.py          # 服务主体
  test_relay.py      # 行为测试（标准库 + 假 NATS）
  Dockerfile         # 只含代码的镜像；配置在运行时挂载
```

部署物（compose、env 模板、NATS ACL 片段、真实的 members.json）包含环境相关值，故意放在运维方的内部仓库，不进这个公开库。

## 设计取舍

- **不上 JetStream**：Agent 只需要一声即时门铃；离线期间漏掉的，靠 Gitea 未读状态这条权威对账来源补。持久化队列会重复存储 Gitea 已经存的东西。
- **只通知、不行动**：不持 Gitea 写权限，不能标已读，不能代替 Agent 发言。
- **配置外置于镜像**：所有环境相关值都挂载/注入，同一个不可变镜像可以在任何环境跑，也才敢公开发布。
