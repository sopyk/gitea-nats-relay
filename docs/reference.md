# 参考：配置项与协议契约

查表用的。全部是**准确值**，改动这里必须同步代码测试（见 ）。

### 实现状态

| 能力 | 状态 |
|---|---|
| HMAC-SHA1/256/512 验签、定长比较 | ✅ 可用 |
| delivery 去重：inflight 占位 / 失败回滚 / LRU 原子落盘 | ✅ 可用 |
| **缺 delivery 头时拒绝**（`400 missing-delivery`） | ✅ 可用 |
| **@提及解析：中文紧邻字符也能匹配**，邮箱/路径不误触 | ✅ 可用 |
| **provider 抽象**：头名 / 验签 / 路由路径 / 事件映射全部来自配置 | ✅ 可用 |
| **双事件头**：`X-Gitea-Event` + `X-Gitea-Event-Type`，细头表优先 | ✅ 可用 |
| **评审事件**：`pull_request_approved` / `_rejected` / `_comment` 不再丢失 | ✅ 可用 |
| **评审正文的 @提及**（读 `review.content`） | ✅ 可用 |
| **请求评审路由**（读顶层 `requested_reviewer` 单数字段） | ✅ 可用 |
| **`ts` 兜底**，不再恒为空串 | ✅ 可用 |
| **成员映射逐条校验**，坏数据不再全员 500 | ✅ 可用 |
| **请求体上限** + Content-Length 防护 + 提前返回时 drain | ✅ 可用 |
| **TOML 类型校验**（漏写中括号、零值、错类型都会报错退出） | ✅ 可用 |
| **成员 host/id 字符校验**（`.` `*` `>` 会导致投错收件位） | ✅ 可用 |
| **去重状态文件类型校验**（内容是合法 JSON 但类型错时不静默失效） | ✅ 可用 |
| **`flush(timeout=…)` 传入真实超时**（不再是 nats-py 的默认 10s） | ✅ 可用 |
| 超时后**仍可能重复投递**（Core NATS 无 ack，窗口无法完全消除） | ⚠️ 见下 |
| **TOML 配置加载 + 环境变量覆盖** | ✅ 可用 |
| **启动配置校验**：未知键 / 非法 kind / 两表冲突告警 | ✅ 可用 |
| **结构化日志**：每请求一行，带 delivery / subject / 耗时；轮转有上限 | ✅ 可用 |
| **日志防注入**：值里的换行/ANSI 不会伪造出一条日志 | ✅ 可用 |
| **日志隐私规则**：只记已投递给 Agent 的字段，不记正文与密钥 | ✅ 可用 |
| **401 限流**（未认证请求刷不爆日志） | ✅ 可用 |
| **subject 前缀校验**：通配符 / 公告与定向撞车，启动即报错 | ✅ 可用 |
| `/healthz`、`/version`、服务心跳、SIGTERM 优雅退出 | ✅ 可用 |
| 更多 provider（GitHub / GitLab） | ⏳ 下一平台 |
| `/metrics`（Prometheus） | ⏳ 等真接了 Prometheus 再做 |

进度与已知缺陷在 dev 分支的 PR 里跟踪。`relay/test_internals.py` 用**照着 Gitea 源码结构体造的 payload** 覆盖了上述每一项，`relay/test_relay.py` 走真实 socket 做端到端验证，`relay/test_logs.py` 钉住日志的降级、隐私与限流行为。

### 配置 —— 所有环境相关值全部外移

镜像里**只有代码和依赖**，不含任何 IP、主机、域名、账号、口令，也不含成员身份。成员映射是只读挂载的文件，密钥走环境变量文件。

### 配置文件形态

配置以**一个 TOML 文件**为准，通过 **bind mount** 进容器；环境变量作为覆盖手段保留（兼容尚未迁移的部署，也承担容器 secret 的角色）。

```
/config/                ← 只读 bind mount（宿主目录 → 容器）
  webhook2nats.toml      服务配置
  members.json           成员身份映射
/data/                   ← 可写卷
  seen.json              去重状态
```

完整模板见 [`config/webhook2nats.example.toml`](../config/webhook2nats.example.toml)，所有值都是占位符，**可以安全地进版本库**；真实配置留在宿主机，不要提交。

**优先级：环境变量 > 配置文件 > 代码默认值。** 环境变量优先，是为了兼容容器 secret 和 CI 注入。

### 为什么是 TOML 而不是 .env / YAML

- **不用 `.env`**：在群晖 File Station 里点开头的文件不能直接编辑，得改名才能读写——配置体验很差。
- **不用 YAML**：需要额外装 PyYAML，破坏「镜像只含代码和依赖」的小体积目标；而且 YAML 会隐式猜类型，`password: 123456` 会被解析成整数、`password: yes` 会被解析成布尔；缩进敏感，从网页复制一段粘贴进来带 Tab 就解析失败。
- **选 TOML**：Python 3.11+ 标准库自带 `tomllib`，**零新增依赖**；支持注释；重复键直接报错而不是静默取最后一个；缩进不敏感。

### 变量 / 配置项对照

配置文件落地期间，环境变量作为覆盖手段继续生效，两者一一对应。
配置文件路径由 `CONFIG_FILE` 决定，默认 `/config/webhook2nats.toml`；**文件不存在时按纯环境变量模式运行**，不会报错。

| 变量 | TOML 键 | 必填 | 说明 |
|---|---|---|---|
| `CONFIG_FILE` | ——（就是它本身） | 否 | 配置文件路径，默认 `/config/webhook2nats.toml` |
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

### Subject 契约

转发器在 NATS 上的账号是服务端 ACL 限权的，**只能**发布这三类：

| Subject | 含义 |
|---|---|
| `<NOTIFY_SUBJECT_PREFIX>.<host>.<id>` | 定向通知某个 Agent（默认前缀 `evt.gitea.notify`） |
| `<BROADCAST_SUBJECT>` | 全厅公告（默认 `evt.gitea.broadcast`）——仅配置作者新建的、带公告标签/标题的 issue |
| `hb.<RELAY_ID>` | 服务心跳（仅当配置了 `RELAY_ID`） |

身份（`<host>.<id>`）编在 subject 里，每个 Agent 的服务端权限模板只锁自己的收件位——**伪造身份会在 NATS 鉴权层直接被拒**，不依赖应用层自觉。

### 消息格式

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
| `issue_comment` | issue 或 PR 下的评论（含 PR 主对话区） |
| `pull_request` | PR 新建 / 同步 / 关闭… |
| `review` | 评审通过 / 驳回 / 评审意见 / **请求评审** |

按事件类型还会多带这些可选字段：`is_pr`（是否为 PR）、`comment_url`、`mentions`、`review_state`、`reviewers`。

> **`ts` 是接收时刻**，不是事件在平台上的发生时间 —— Gitea 的 webhook payload
> 里没有时间字段。上面示例里的 `"2026-10-10T09:30:00Z"` 就是真实会出现的格式。

### HTTP 契约

**所有响应都是 JSON**，`Content-Type: application/json; charset=utf-8`。

- 成功（`2xx`）给 `status` 字段，失败给 `error` 字段。
- 多数响应带 `delivery`（原样回显请求的 delivery 头）—— 平台侧可以直接拿它
  和日志里的 `delivery=` 对上号。
- 多数错误额外带 `hint`，说明**该改哪里的配置**，不用翻代码。

- `GET /healthz` → NATS 连通返回 `200`（`status` 为 `ok`），否则 `503`（`error` 为 `nats-down`）
- `GET /version` → `200`，`status` 为 `ok`，另返回 `version`（发布版本号）、
  `commit`（精确 commit）、`started_at` 与 `uptime_seconds`
- `POST /gitea-hook`（Gitea webhook 配置的回调地址，路径由 provider 的 `path` 决定）：

| 状态 | `status` / `error` | 含义 | 平台的下一步 |
|---|---|---|---|
| `200` | `accepted` | 所有相关 subject 已发布并 flush 成功 | 记为成功。**只有这时**才把 delivery 标记为已处理 |
| `200` | `duplicate` | 这个 delivery 早先已成功处理过 | 记为成功，不再重试 |
| `200` | `ignored` | 事件类型不在映射表里（push 等我们不关心的） | 记为成功，不再重试 |
| `400` | `bad-json` | 请求体不是合法 JSON 对象 | 视为失败 |
| `400` | `bad-length` | `Content-Length` 非法 | 视为失败 |
| `400` | `missing-delivery` | 缺少 delivery 头，无法幂等 | 视为失败。**请把 Webhook 配置补全** |
| `401` | `bad-signature` | 签名不匹配或没配密钥 | 视为失败 |
| `404` | `not-found` | 路径没有对应的 provider | 视为失败 |
| `409` | `inflight` | 同一 delivery 正在被处理 | 可能重试 |
| `413` | `payload-too-large` | 请求体超过 1 MB | 视为失败 |
| `500` | `publish-error` / `internal-error` | 未预期异常 | 视为失败 |
| `502` | `publish-failed` | 某个 subject 发布失败 | **用同一个 UUID 重试** |
| `504` | `publish-timeout` | 等待 NATS 确认超时 | **用同一个 UUID 重试** |

例：

```json
{"error": "missing-delivery", "hint": "平台必须带 delivery 头，否则无法去重"}
```

**`502` / `504` / `500` 时 delivery 会回滚** —— 平台重试不会被当成重复丢弃。所有失败路径都保证释放 inflight 占位，所以平台重投总能被正常处理，不会永远卡在 `409`。

> 路径是按 provider 配的。换一个平台就是换 `path`，核心契约不变。

### 去重与重试语义

delivery UUID 先进入 `inflight` 状态，**只有在所有 NATS 发布 + flush 都成功后**才落盘为已处理。失败或超时就回滚，让来源平台安全地用同一个 `X-Gitea-Delivery` 重试；重叠重试同 UUID 返回 `409`。状态是 LRU（默认 2000 条）并原子落盘，重启不会重复通知。

### @提及解析

抽取 `@登录名` 时**先剥离代码块和行内代码**——所以反引号或代码块里的 `@bob` 不会触发通知。只有出现在成员映射里的登录名才会生成定向 subject；不在映射里的名字不路由，但会保留在事件的 `mentions` 里供展示。

边界的判定用**显式的 ASCII 字符类**，不用 `\b` / `\w`：Python 3 的 `\w` 是 Unicode 感知的，汉字属于 `\w`，于是「请@bob看」「@bob请看」会被误判成「`@` 紧邻单词字符」而丢掉——而中文正文里那才是常态。

| 写法 | 结果 | 说明 |
|---|---|---|
| `请 @bob 看` | ✅ | 空格边界 |
| `请@bob看` | ✅ | 汉字边界 |
| `@bob，请跟进` | ✅ | 全角标点边界 |
| `` `@bob` 是代码 `` | ❌ 不触发 | 行内码被剥离 |
| `user@example.com` | ❌ 不触发 | 邮箱 |
| `src/@file` | ❌ 不触发 | 路径 |

