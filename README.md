# Webhook2NATS

一个轻量的 **Webhook → Core NATS** 实时门铃转发器。

**它是一个通用通道，不属于任何一个平台。** 平台通过 Webhook 推事件，这个通道负责校验签名、去重、解析「这次事件该通知谁」，然后把一个精简事件发布到 NATS，让订阅的 Agent 毫秒级被叫醒——而不是靠轮询定时抓。

Gitea / Forgejo 是**第一个接入方**，本文档以它作为完整示例；GitHub、GitLab、Jenkins 或任何会发 Webhook 的系统都能接进来。

- **运行环境**：Python 3.12 + [`nats-py`](https://github.com/nats-io/nats.py)，打成很小的 Alpine 镜像。
- **传输**：纯 **Core NATS**（即时、at-most-once、纯内存）。**没有 JetStream、不存消息历史、没有数据库**。
- **定位**：只做通知。它**不存正文、不标已读、不代替任何 Agent 行动**。来源平台始终是事实源；Agent 被叫醒后自己去那边拉完整内容。

---

## 实现状态

本文档描述的是**目标设计**。dev 分支正在向它推进，下表是当前的真实进度。

| 能力 | 状态 |
|---|---|
| HMAC-SHA256 验签、定长比较 | ✅ 可用 |
| delivery 去重：inflight 占位 / 失败回滚 / LRU 原子落盘 | ✅ 可用 |
| @提及解析（先剥离代码块与行内码） | ✅ 可用 ⚠️ 见下方说明 |
| 成员映射路由、公告判定 | ✅ 可用 |
| Core NATS 发布 + flush 确认 | ✅ 可用 |
| `GET /healthz`、服务心跳、SIGTERM 优雅退出 | ✅ 可用 |
| **配置文件（TOML）加载 + 环境变量覆盖** | 🚧 dev 进行中 |
| **provider 抽象（事件头 / delivery 头 / 验签 / 事件映射可配）** | 🚧 dev 进行中 |
| **`/config` 只读 bind mount 约定** | 🚧 dev 进行中 |

标 🚧 的部分描述的是设计，代码尚未落地；以代码现状为准。进度与已知缺陷在 dev 分支的 PR 里跟踪。

> ⚠️ **`@提及` 解析目前只在 `@登录名` 前后是空格或 ASCII 标点时生效。**
> 中文正文里常见的「请@某人看」这种紧邻写法会被漏掉。这条正在 dev 上修，
> 见「@提及解析」一节。

---

## 为什么要做

一队 Agent 每几分钟轮询一次自建的 Gitea：空请求很多，延迟最多一个轮询周期。这个转发器插在来源平台和 NATS 中间——平台每个事件推一次 Webhook，转发器校验、压缩、解析收件人，然后往 NATS 发一个「门铃」。在线的 Agent 毫秒级收到；离线的 Agent 本来就收不到（这是 Core NATS 的设计），由另一条**低频巡厅**兜底对账（对账依据是来源平台的未读状态）。

## 架构

```
┌──────────────┐  webhook（HTTP POST，平台各自的签名算法）  ┌──────────────────┐  Core NATS   ┌──────────────────┐
│  Gitea /     │ ────────────────────────────────────────▶ │   Webhook2NATS    │ ───────────▶ │  订阅的 Agent     │
│  GitHub / …  │  issues / issue_comment /                 │  （通用通道）      │  门铃 subject │ （在线时收到）    │
│              │  pull_request / pull_request_review       │                    │              └──────────────────┘
└──────────────┘                                           └──────────────────┘
         ▲                                                          │
         │ Agent 自己去拉完整 issue/PR 内容                          │ provider 适配 + 去重状态
         └──────────────────────────────────────────────────────────┘
```

每个事件的处理流程：

1. **来源平台**发出 webhook；
2. **Webhook2NATS** 校验签名、按 delivery UUID 去重、依据**外部挂载的成员映射**解析收件人（@提及 / 指派 / 请求评审），然后发布一个精简 JSON 事件（控制在 1 KB 以内）；
3. **NATS** 扇出给匹配的在线订阅者。

如果没有订阅者在线，Core NATS 按设计丢弃——低频巡厅是保证不漏的兜底。**这个服务故意不做持久化存储。**

---

## 通用模型：接入一个平台要做哪几件事

真正和平台绑定的只有一小块：

> **路由路径 / 事件头字段名 / delivery 头字段名 / 验签算法 / 原始 payload 的归一化**

其余全部是平台无关的通用能力：HTTP 接收、delivery 幂等去重、按成员映射路由、发布到 NATS 并等待确认。

接入一个新平台（GitHub / GitLab / Jenkins / …）= 声明五件事：

| # | provider 要声明 | 说明 |
|---|---|---|
| 1 | 路由路径 | Webhook 回调打在本服务的哪个 URL 上 |
| 2 | 事件类型头 | 事件类型写在哪个头字段 |
| 3 | delivery 头 | 投递唯一 ID 在哪个头字段 |
| 4 | 验签算法 | 该平台用什么算法、签名放在哪个头 |
| 5 | 事件映射 | 平台的原始事件名 → 统一事件类型；payload 归一化成统一字段 |

**前四件是声明式的，直接写配置；第五件的后半段（payload 归一化）是需要写代码的**，因为每个平台的 JSON 结构都不一样。归一化之后进入核心逻辑的部分完全一致。

### 以 Gitea 为例

Gitea 走 JSON Webhook，验签用 `X-Gitea-Signature` 放 HMAC-SHA256 十六进制串，delivery ID 在 `X-Gitea-Delivery`，事件类型在 `X-Gitea-Event`。对应配置见 [`config/webhook2nats.example.toml`](config/webhook2nats.example.toml)：

```toml
[providers.gitea]
enabled = true
path = "/gitea-hook"

[providers.gitea.headers]
event = "X-Gitea-Event"
delivery = "X-Gitea-Delivery"
signature = "X-Gitea-Signature"

[providers.gitea.signature]
algorithm = "hmac-sha256"
secret = "<与 Gitea webhook 配置里同一个密钥>"

[providers.gitea.events]
issues = "issue"
issue_comment = "issue_comment"
pull_request = "pull_request"
pull_request_approved = "review"
pull_request_rejected = "review"
pull_request_comment = "review"
```

> **写事件映射时有个坑，务必照着平台实际发出的 header 值写。**
> Gitea 发送的是 `X-Gitea-Event: <EventType.Event()>`，而评审类事件实际发出的是
> `pull_request_approved` / `pull_request_rejected` / `pull_request_comment`
> 三个不同的值——**并不存在 `pull_request_review` 这个 header 值**，
> 后者只是 Gitea 建 webhook 时用的分组配置键。照抄分组键会静默丢掉全部评审事件
> （服务返回 `200 ignored:...`，平台会认为投递成功且永不重试）。
>
> 通用教训：**事件映射要对着平台发出的实际 header 取值来写，不要照着平台的文档分类名写。**

Gitea 的 payload 归一化（第五件的后半段）就是把 `repository` / `sender` / `issue` / `pull_request` / `comment` / `review` 这些平台特有字段，收敛成下面那份统一事件。

目前内置的是 `gitea`。这套接口是稳定的——这是本服务设计成通用转发器的关键。

---

## 配置 —— 所有环境相关值全部外移

镜像里**只有代码和依赖**，不含任何 IP、主机、域名、账号、口令，也不含成员身份。成员映射是只读挂载的文件，密钥走环境变量文件。

### 🚧 配置文件形态（dev 进行中）

配置以**一个 TOML 文件**为准，通过 **bind mount** 进容器；环境变量作为覆盖手段保留。

```
/config/                ← 只读 bind mount（宿主目录 → 容器）
  webhook2nats.toml      服务配置
  members.json           成员身份映射
/data/                   ← 可写卷
  seen.json              去重状态
```

完整模板见 [`config/webhook2nats.example.toml`](config/webhook2nats.example.toml)，所有值都是占位符，**可以安全地进版本库**；真实配置留在宿主机，不要提交。

**优先级：环境变量 > 配置文件 > 代码默认值。** 环境变量优先，是为了兼容容器 secret 和 CI 注入。

### 为什么是 TOML 而不是 .env / YAML

- **不用 `.env`**：在群晖 File Station 里点开头的文件不能直接编辑，得改名才能读写——配置体验很差。
- **不用 YAML**：需要额外装 PyYAML，破坏「镜像只含代码和依赖」的小体积目标；而且 YAML 会隐式猜类型，`password: 123456` 会被解析成整数、`password: yes` 会被解析成布尔；缩进敏感，从网页复制一段粘贴进来带 Tab 就解析失败。
- **选 TOML**：Python 3.11+ 标准库自带 `tomllib`，**零新增依赖**；支持注释；重复键直接报错而不是静默取最后一个；缩进不敏感。

### 变量 / 配置项对照

配置文件尚未落地期间，现有环境变量继续生效，两者一一对应：

| 变量 | TOML 键 | 必填 | 说明 |
|---|---|---|---|
| `NATS_SERVERS` | `[nats] servers` | 是 | NATS 地址，多个用逗号分隔或写成数组 |
| `NATS_USER` / `NATS_PASSWORD` | `[nats] user` / `password` | 是 | 转发器专用 NATS 账号（ACL 已限权到门铃 subject） |
| `WEBHOOK_SECRET` | `[providers.gitea.signature] secret` | 是 | HMAC 密钥，与 Gitea webhook 配置里同一个 |
| `MEMBERS_FILE` | `[routing] members_file` | 否 | 登录名→`{host,id}` 映射 JSON，默认 `/config/members.json`（只读挂载） |
| `DEDUP_FILE` | `[dedup] file` | 否 | delivery 去重状态，默认 `/data/seen.json`（可写卷） |
| `RELAY_ID` | `[service] relay_id` | 否 | 心跳后缀（`hb.<RELAY_ID>`）；不填则不发心跳 |
| `RELAY_CLIENT_NAME` | `[service] client_name` | 否 | NATS 连接显示名，默认 `webhook2nats` |
| `NATS_HEARTBEAT_SECONDS` | `[service] heartbeat_seconds` | 否 | 心跳间隔，默认 `60` |
| `LISTEN` | `[service] listen` | 否 | HTTP 监听地址，默认 `0.0.0.0:8090` |
| `PUBLISH_TIMEOUT_SECONDS` | `[service] publish_timeout_seconds` | 否 | 等待 NATS 发布确认，默认 `3` |
| `BROADCAST_AUTHORS` | `[routing] broadcast_authors` | 是* | 这些作者的新 issue 带公告标签/标题时走全厅 |
| `NOTIFY_SUBJECT_PREFIX` | `[routing] notify_subject_prefix` | 否 | 默认 `evt.gitea.notify` |
| `BROADCAST_SUBJECT` | `[routing] broadcast_subject` | 否 | 默认 `evt.gitea.broadcast` |

\* 代码里**没有内置任何默认作者**；不配就等于不发全厅公告。

`members.json` 示例（mount 到 `/config/members.json`，**不在镜像里**）：

```json
{
  "members": {
    "alice": { "host": "h1", "id": "alice" },
    "bob":   { "host": "h2", "id": "bob" }
  }
}
```

---

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

`kind` 的取值集合：

| kind | 触发事件 |
|---|---|
| `issue` | issue 新建 / 重开 / 关闭 / 指派 / 标签变更… |
| `issue_comment` | issue 或 PR 下的评论 |
| `pull_request` | PR 新建 / 同步 / 关闭… |
| `review` | 评审通过 / 驳回 / 评审意见 |

按事件类型还会多带这些可选字段：`is_pr`（是否为 PR）、`comment_url`、`mentions`、`review_state`、`reviewers`。

## HTTP 契约

- `GET /healthz` → NATS 连通返回 `200 ok`，否则 `503 nats-down`
- `POST /gitea-hook`（Gitea webhook 配置的回调地址）：
  - `401 bad-signature` — HMAC 不匹配或没配 `WEBHOOK_SECRET`
  - `200 duplicate` — 该 delivery 已成功处理过
  - `409 inflight` — 同一 delivery 正在被一个重叠的重试处理
  - `400 bad-json` — 请求体不是合法 JSON
  - `502 publish-failed` / `504 publish-timeout` / `500 publish-error` — NATS 未确认发布；delivery **回滚**，让 Gitea 重试时复用同一 UUID，而不是被当重复丢弃
  - `200 accepted` — 所有相关 subject 都已发布并 flush 完成；**只有这时**才把 delivery 标记为已处理

> 路径是按 provider 配的。换一个平台就是换 `path`，核心契约不变。

### 去重与重试语义

delivery UUID 先进入 `inflight` 状态，**只有在所有 NATS 发布 + flush 都成功后**才落盘为已处理。失败或超时就回滚，让来源平台安全地用同一个 `X-Gitea-Delivery` 重试；重叠重试同 UUID 返回 `409`。状态是 LRU（默认 2000 条）并原子落盘，重启不会重复通知。

### @提及解析

抽取 `@登录名` 时**先剥离代码块和行内代码**——所以反引号或代码块里的 `@bob` 不会触发通知。只有出现在成员映射里的登录名才会生成定向 subject；不在映射里的名字不路由，但会保留在事件的 `mentions` 里供展示。

> ⚠️ 🚧 边界判定正在重写。当前实现要求 `@登录名` 前后是空格或 ASCII 标点，
> 中文正文里「请@某人看」「@某人，请跟进」这类紧邻写法会漏掉。
> 目标行为：`@` 前后为空白、ASCII 标点、CJK 字符或全角标点时都视为有效边界，
> 但仍要排除邮箱地址（`user@example.com`）和路径。

## 安全说明

- 转发器**不持有 Gitea token**，用的 NATS 账号也只能发布三个 subject、不能读消息、不能冒充任何 Agent。即使被打穿，影响面仅限于伪造门铃——而门铃里只有公开元数据和 URL，没有正文。
- webhook 用 HMAC-SHA256 校验；监听端应放在内网 / tailnet，不暴露公网。
- 密钥在运维方的 env 文件里（`0600`），成员映射是只读挂载。两者都不进镜像、也不进这个仓库。
- bind mount 的 `/config` 建议只读（`:ro`），口令建议用容器 secret 经环境变量注入而非落在文件里。

## 测试

不需要真实的 NATS 或网络——测试里用假的 NATS 客户端、直接调 HTTP handler：

```bash
cd relay
python3 test_relay.py
```

覆盖：HMAC 拒绝、代码块内 @ 不误触、成员映射路由、公告判定、delivery 去重往返、失败重试、inflight 拒绝、端到端 webhook 处理。

> 🚧 上面的覆盖清单是**目标**。当前测试通过替换掉 NATS 客户端来跑，
> 成员映射路由的 subject 拼接、provider 事件映射、payload 归一化这几段
> 尚未被直接覆盖。dev 分支会补上以真实平台 payload 为 fixture 的用例。

## 目录结构

```
relay/
  server.py          # 服务主体
  test_relay.py      # 行为测试（标准库 + 假 NATS）
  Dockerfile         # 只含代码的镜像；配置在运行时挂载
config/
  webhook2nats.example.toml   # 配置模板（无环境值，可进库）
```

部署物（compose、真实配置、真实的 members.json、NATS ACL 片段）包含环境相关值，故意放在运维方的内部仓库，不进这个公开库。**配置模板**不含环境值，所以可以公开。

## 部署

```bash
docker run -d --name webhook2nats \
  -p 8090:8090 \
  -v /宿主机/path/config:/config:ro \
  -v webhook2nats-data:/data \
  -e WEBHOOK_SECRET \
  --env-file relay.env \
  webhook2nats:latest
```

`/config` 只读挂载配置与成员映射，`/data` 可写卷放去重状态。`/healthz` 可直接做容器健康检查。

## 设计取舍

- **平台适配做成声明式配置，而不是运行时插件**：前四件事（路径、三个头名、验签算法）各平台差异很小且稳定，写死在代码里每次接新平台都要改代码；声明式配置把它变成一次 diff。payload 归一化确实各平台不同，那部分保留为代码，不硬套进配置。
- **TOML 而不是 .env / YAML**：零新增依赖、重复键报错、口令不会被隐式转型、对 Tab 不敏感——后者对 NAS 上手工编辑配置很关键。
- **不上 JetStream**：Agent 只需要一声即时门铃；离线期间漏掉的，靠来源平台未读状态这条权威对账来源补。持久化队列会重复存储平台已经存的东西。
- **只通知、不行动**：不持平台写权限，不能标已读，不能代替 Agent 发言。
- **配置外置于镜像**：所有环境相关值都挂载/注入，同一个不可变镜像可以在任何环境跑，也才敢公开发布。