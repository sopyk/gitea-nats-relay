# Webhook2NATS

一个轻量的 **Webhook → Core NATS** 实时门铃转发器。

**它是一个通用通道，不属于任何一个平台。** 平台通过 Webhook 推事件，这个通道负责校验签名、去重、解析「这次事件该通知谁」，然后把一个精简事件发布到 NATS，让订阅的 Agent 毫秒级被叫醒——而不是靠轮询定时抓。

Gitea / Forgejo 是**第一个接入方**，本文档以它作为完整示例；GitHub、GitLab、Jenkins 或任何会发 Webhook 的系统都能接进来。

- **运行环境**：Python 3.12 + [`nats-py`](https://github.com/nats-io/nats.py)，打成很小的 Alpine 镜像。
- **传输**：纯 **Core NATS**（即时、at-most-once、纯内存）。**没有 JetStream、不存消息历史、没有数据库**。
- **定位**：只做通知。它**不存正文、不标已读、不代替任何 Agent 行动**。来源平台始终是事实源；Agent 被叫醒后自己去那边拉完整内容。

---

## 实现状态

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
| **日志隐私规则**：只记已投递给 Agent 的字段，不记正文与密钥 | ✅ 可用 |
| **401 限流**（未认证请求刷不爆日志） | ✅ 可用 |
| **subject 前缀校验**：通配符 / 公告与定向撞车，启动即报错 | ✅ 可用 |
| `/healthz`、`/version`、服务心跳、SIGTERM 优雅退出 | ✅ 可用 |
| 更多 provider（GitHub / GitLab） | ⏳ 下一平台 |
| `/metrics`（Prometheus） | ⏳ 等真接了 Prometheus 再做 |

进度与已知缺陷在 dev 分支的 PR 里跟踪。`relay/test_internals.py` 用**照着 Gitea 源码结构体造的 payload** 覆盖了上述每一项，`relay/test_relay.py` 走真实 socket 做端到端验证，`relay/test_logs.py` 钉住日志的降级、隐私与限流行为。

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

## 跑一遍：一个真实的 Gitea 事件

> 下面这一节用一个具体事件把整条链路走一遍。所有时间、ID、URL 都是示意值，
> 但**字段名、subject 结构、状态码都是真实契约**。

**场景**：09:30:00，你在 `org/repo` 的 issue #134 里 @ 了一个 Agent——alice。

### 09:30:00.003 — Gitea 发出 Webhook

Gitea 把事件 POST 到你配的回调地址（本服务默认 `POST /gitea-hook`）：

```http
POST /gitea-hook HTTP/1.1
X-Gitea-Event: issue_comment
X-Gitea-Delivery: 6f1c0a2e-8b3d-4f7a-9c1e-2d5b7a0e4f83
X-Gitea-Signature: 3f2a9d1c...            ← HMAC-SHA256(request_body)

{
  "action": "created",
  "repository": { "full_name": "org/repo",
                  "html_url": "https://gitea.example/org/repo" },
  "sender": { "login": "you" },
  "issue": {
    "number": 134,
    "title": "评审流水线的重试策略",
    "html_url": "https://gitea.example/org/repo/issues/134",
    "state": "open",
    "labels": []
  },
  "comment": {
    "html_url": "https://gitea.example/org/repo/issues/134#issuecomment-42",
    "body": "@alice 这个重试上限你看要不要设成 3？"
  }
}
```

三个头各司其职：**事件类型**决定走哪条归一化逻辑，**delivery** 是幂等去重的依据，**签名**是信任边界。

### 09:30:00.005 — 转发器做的四件事

1. **验签** —— 用 `WEBHOOK_SECRET` 对**原始请求体**重算 HMAC-SHA256，`hmac.compare_digest` 定长比较（不用定长比较就能用时序侧信道逐字节爆破）。→ 通过
2. **去重占位** —— `6f1c0a2e…` 进 inflight 集合。此刻还**不算**已处理，只是占住坑位防止重试并发投递。
3. **剥代码 → 抽 @提及** —— 先把 ```` ``` ```` 代码块和 `` ` `` 行内码整段抹掉（防止把示例代码里的 `@alice` 当真），再从剩下的正文抽 `@alice`。
4. **查成员映射** —— `members.json` 里 `alice → {host: "h1", id: "alice"}`，拼出收件位：

```
evt.gitea.notify.h1.alice
   └──前缀──┘ └host┘ └id┘
```

> `@某人` 不在成员映射里**不会路由**（也就不会打扰到无关的人），但名字会留在事件的 `mentions` 字段里，供界面展示「这条提到了但没部署 Agent 的人」。

### 09:30:00.011 — 门铃上线

relay 把精简事件发到 NATS，然后 `flush()` 等服务器确认。**只有确认成功**，才把这个 delivery 落盘成「已处理」，并回 `200 accepted`。

alice 那边收到的就是这一条：

```json
{
  "kind": "issue_comment",
  "action": "created",
  "repo": "org/repo",
  "actor": "you",
  "ts": "2026-10-10T09:30:00Z",
  "number": 134,
  "title": "评审流水线的重试策略",
  "url": "https://gitea.example/org/repo/issues/134",
  "state": "open",
  "assignees": [],
  "labels": [],
  "comment_url": "https://gitea.example/org/repo/issues/134#issuecomment-42",
  "mentions": ["alice"],
  "delivery": "6f1c0a2e-8b3d-4f7a-9c1e-2d5b7a0e4f83"
}
```

**注意里面没有「这个重试上限你看要不要设成 3」这句话。** relay 刻意不搬运正文——门铃只负责说一声「134 号 issue 上有个跟你有关的动静」，正文由 alice 自己回 Gitea 拉。这是它能被摆在打穿影响面里的前提：即使伪造门铃，里面也只有公开元数据和 URL。

> `ts` 是接收时刻的 ISO-8601 时间戳。Gitea 的 webhook payload 里**没有**时间字段，
> 所以这里取的是 relay 收到请求的时刻 —— 够 Agent 排序和判断新鲜度，不代表事件在平台上的真实发生时间。

### 09:30:00.015 — alice 醒来

从收到门铃到她能开工，中间隔着一次 Gitea API 调用（拉 issue 全文 + 评论 + 关联 PR）。从你按下回车到 alice 被叫醒，**十几毫秒**；对比原来 5 分钟一轮的轮询，延迟上限从「一个轮询周期」降到「一个网络往返」。

### 如果 alice 当时不在线？

Core NATS 没有订阅者就丢弃，这是设计而不是 bug。alice 下次上线**不会**收到这条门铃——**由另一条低频巡厅兜底**，对账依据是 Gitea 的未读状态。

两者的分工要分清：**转发器管「快」，巡厅管「不漏」。** 谁也替代不了谁，所以这个服务才敢不做持久化。

### 失败了会怎样

NATS 没确认（连接断了、flush 超时）时，relay 回的**不是** `200`：

| relay 返回 | 含义 | Gitea 会怎么做 | relay 这边 |
|---|---|---|---|
| `200 accepted` | 全部 subject 已发布并 flush 成功 | 记为投递成功 | **现在才**把 delivery 落盘成已处理 |
| `200 duplicate` | 这个 delivery 早先已经成功处理过 | 记为成功，不再重试 | 丢弃，不重复叫醒 |
| `409 inflight` | 上一次重试还在处理中 | 可能重试 | 不动，等前一次出结果 |
| `502 publish-failed` | 某个 subject 发布失败 | 认为失败，**用同一个 UUID 重试** | **回滚** inflight 占位，这次不算已处理 |
| `504 publish-timeout` | 等待 NATS 确认超时 | 同上 | 同上 |
| `401 bad-signature` | 验签不过 | 认为失败 | 直接拒，不落任何状态 |

关键在于**先发布成功、再记已处理**。反过来做的话，一次网络抖动就会让 alice 永远收不到这条通知，而且没有任何补救机会——因为平台已经被告知「投递成功」，不会再试了。

> ⚠️ **超时路径上有一个无法消除的重复投递窗口**
>
> Core NATS 没有 ack 机制：`publish()` 一执行，字节就写进 socket 了，
> 后面的 `flush()` 只是在等服务器回 PONG。所以当 NATS 卡住、我们放弃等待并回 `504` 时，
> 那条消息**可能其实已经上线了**。平台收到 `504` 会用同一 UUID 重试，于是 alice 可能收到两次。
>
> 能做的只是把窗口压到最小：`flush()` 的超时被设成 HTTP 预算的 80%，
> 让 NATS 先于 HTTP 层放弃。`future.cancel()` 只能中止「等 PONG」，
> 撤不回已经发出去的字节 —— 这一点没有捷径。
>
> **换句话说：去重保证的是「不重复处理」，不是「恰好一次投递」。**
> 下游 Agent 若要严格幂等，应当用 `delivery` 字段自己去重。

### 自己敲一遍

不用真配 Gitea 也能验证整条链路。先订阅 alice 的收件位：

```bash
nats sub 'evt.gitea.notify.h1.alice'
```

再手工发一个签名正确的 webhook：

```bash
# 用 heredoc 写 body，签名必须对**文件里的确切字节**计算
cat > /tmp/hook.json <<'JSON'
{"action":"created",
 "repository":{"full_name":"org/repo"},
 "sender":{"login":"you"},
 "issue":{"number":134,"title":"t",
          "html_url":"https://g.example/org/repo/issues/134","state":"open","labels":[]},
 "comment":{"html_url":"https://g.example/org/repo/issues/134#issuecomment-42",
            "body":"@alice 帮忙看看"}}
JSON

SIG=$(openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" /tmp/hook.json | sed 's/^.* //')

curl -i -X POST http://127.0.0.1:8090/gitea-hook \
  -H "Content-Type: application/json" \
  -H "X-Gitea-Event: issue_comment" \
  -H "X-Gitea-Delivery: 11111111-2222-3333-4444-555555555555" \
  -H "X-Gitea-Signature: $SIG" \
  --data-binary @/tmp/hook.json
```

拿到 `200 accepted`，另一个终端的 `nats sub` 会立刻打印出门铃 JSON。

**把同一个 `X-Gitea-Delivery` 再发一遍** —— 会拿到 `200 duplicate`，且 `nats sub` 那边**不会**多出一条。这是在本地就能验证去重是否生效的最快方法。

> 两个容易踩的点：
> - 用 `--data-binary` 而不是 `-d`：后者会吃掉文件里的换行，导致实际发出去的字节和算签名的字节不一致，恒定 401。
> - `X-Gitea-Signature` 是**请求体的**签名，不是 URL 或某个字段的签名。Gitea 另外还会发 `X-Hub-Signature`(SHA-1) 和 `X-Hub-Signature-256`(带 `sha256=` 前缀)，本服务只认 `X-Gitea-Signature`。

---

## 通用模型：接入一个平台要做哪几件事

真正和平台绑定的只有一小块：

> **路由路径 / 事件头字段名 / delivery 头字段名 / 验签算法 / 原始 payload 的归一化**

其余全部是平台无关的通用能力：HTTP 接收、delivery 幂等去重、按成员映射路由、发布到 NATS 并等待确认。

接入一个新平台（GitHub / GitLab / Jenkins / …）= 声明六件事：

| # | provider 要声明 | 说明 |
|---|---|---|
| 1 | 路由路径 | Webhook 回调打在本服务的哪个 URL 上 |
| 2 | 粗事件头 | 归一化组名写在哪个头字段 |
| 3 | 细事件头 | 精确触发器写在哪个头字段（**可选**，见下） |
| 4 | delivery 头 | 投递唯一 ID 在哪个头字段 |
| 5 | 验签算法 | 该平台用什么算法、签名放在哪个头 |
| 6 | 事件映射 | 平台的原始事件名 → 统一事件类型；payload 归一化成统一字段 |

**前五件是声明式的，直接写配置；第六件的后半段（payload 归一化）是需要写代码的**，因为每个平台的 JSON 结构都不一样。归一化之后进入核心逻辑的部分完全一致。

#### 为什么事件头要分粗细两个

有些平台会把多个触发器**折叠成同一个组名**。以 Gitea 为例，下面三件事在 `X-Gitea-Event` 里**全都是 `pull_request`**，只有 `X-Gitea-Event-Type` 才区分得开：

| 实际发生的事 | `X-Gitea-Event`（粗） | `X-Gitea-Event-Type`（细） |
|---|---|---|
| 张三开了这个 PR | `pull_request` | `pull_request` |
| 李四被**指派为评审人** | `pull_request` | `pull_request_review_request` |
| 王五往这个 PR **推了新 commit** | `pull_request` | `pull_request_sync` |

这直接决定「该通知谁」——指派评审该通知李四，推 commit 该通知 assignee。只读粗头就分不开，README 承诺的**「请求评审」路由就做不到**。

粗头的存在是有理由的（Gitea 配置界面里订阅「Pull Request」是一栏勾选，勾上后所有子触发器都跟着来），细头是用来还原细节的。

**细头槽位是可选的。** GitHub 只有一个 `X-GitHub-Event`，而且它本身就是精确的（`pull_request_review` 和 `pull_request_review_comment` 是分开的），压根不需要粗细之分。这类平台把 `headers.event_type` 整行删掉即可，`[providers.X.event_types]` 随之不生效，自动退化成单表匹配——**通用接口不该被 Gitea 的形状绑死**。

### 以 Gitea 为例

Gitea 走 JSON Webhook，验签用 `X-Gitea-Signature` 放 HMAC-SHA256 十六进制串，delivery ID 在 `X-Gitea-Delivery`。完整配置见 [`config/webhook2nats.example.toml`](config/webhook2nats.example.toml)：

```toml
[providers.gitea]
enabled = true
path = "/gitea-hook"

[providers.gitea.headers]
event = "X-Gitea-Event"
event_type = "X-Gitea-Event-Type"
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

[providers.gitea.event_types]
pull_request_review_request = "review"
```

匹配顺序：**先查 `[event_types]`（细头），未命中再查 `[events]`（粗头），都没命中才 `200 ignored:`**。所以 `[event_types]` 里只写「与 `[events]` 结果不同」的条目就行。

#### Gitea 的完整事件头对照表

这张表是配事件映射的依据。以下对照 Gitea `main` 分支的 `modules/webhook/type.go` 与 `services/webhook/deliver.go` 推导：

| `X-Gitea-Event-Type`（精确触发器） | → | `X-Gitea-Event`（组名） | 只靠粗头能区分吗 |
|---|---|---|---|
| `pull_request_review_approved` | → | `pull_request_approved` | ✅ 粗头已独立 |
| `pull_request_review_rejected` | → | `pull_request_rejected` | ✅ 粗头已独立 |
| `pull_request_review_comment` | → | `pull_request_comment` | ✅ 粗头已独立 |
| `pull_request_review_request` | → | `pull_request` | ❌ **必须读细头** |
| `pull_request_sync` | → | `pull_request` | ❌ 必须读细头 |
| `pull_request_assign` / `_label` / `_milestone` | → | `pull_request` | ❌ 必须读细头 |
| `issue_assign` / `issue_label` / `issue_milestone` | → | `issues` | ❌ 必须读细头 |
| `pull_request_comment` | → | `issue_comment` | ❌ 必须读细头 |
| `push` `create` `delete` `fork` `wiki` `repository` `release` `package` `status` `workflow_run` `workflow_job` | → | 同名 | ✅ 粗头已独立 |

> ⚠️ **两个会导致静默丢事件的坑**
>
> **1. 别照抄文档里的分类名。** Gitea 从不投递 header 值 `pull_request_review`——那只是配置界面的分组伞事件名。上表里评审三事件在粗头中各自独立，所以 `[events]` 里那三行是对的，**不要因为「粗头是组名」就以为它们会折叠而删掉**。
>
> **2. `pull_request_comment` 在两个头里含义不同。** 同一个字符串：
> - 出现在 `X-Gitea-Event` 里 → 来自 `pull_request_review_comment`，即**评审意见**，应映射成 `review`
> - 出现在 `X-Gitea-Event-Type` 里 → 来自 `pull_request_comment`，即 **PR 主对话区的评论**，应映射成 `issue_comment`
>
> **每张表匹配哪个头必须写清楚**，否则以后把映射切到细头上就会悄悄改错。

### 配置校验的风险

两张表的设计有几条已知风险，都靠**启动时的配置校验**兜底（已实现）：

| 风险 | 后果 | 缓解 |
|---|---|---|
| 两张表同一键值不同 | 改 `[events]` 忘了改 `[event_types]`，**改动静默不生效** | 启动时打 WARN，点名哪个实际生效 |
| **表名写错** | TOML 只校验语法不校验 schema，`[event_type]`（少个 s）会被当成无关新表，**细粒度路由全丢**且无报错 | 启动时校验未知键并拒绝启动 |
| `kind` 值不校验语义 | 填进组名而非事件类型，下游收到永不出现的 `kind` | 启动时对白名单校验 |
| **类型写错** | `broadcast_authors = "alice"`（漏了中括号）→ `{'a','l','i','c','e'}`，**全厅公告永久静默失效** | 启动时校验类型，拒绝启动 |
| **数值为 0** | `heartbeat_seconds = 0` → `asyncio.sleep(0)` 变成打 NATS 的忙循环 | 启动时校验范围 |
| 配置文件不完整 | 只写 `[nats]` 不写 providers，事件映射悄悄变空 | 拒绝启动，不静默退回默认 |
| **成员 host/id 含非法字符** | `id = "a.b"` → `evt.gitea.notify.h1.a.b`，**那是另一个 Agent 的收件位，门铃直接投错人** | 只允许 `[A-Za-z0-9_-]`，其余拒绝 |
| 接口被 Gitea 形状污染 | 只有单头的平台被迫配一张永远空的表 | `event_type` / `event_types` 均可选 |

最后一条同样是「`members.json` 无 schema 校验」的同类问题，已一并解决：成员映射现在逐条校验，一个非 dict 值会让**整个文件**被拒绝并打印是哪个登录名有问题，而不是让每一次 webhook 抛异常。

Gitea 的 payload 归一化（第六件的后半段）就是把 `repository` / `sender` / `issue` / `pull_request` / `comment` / `review` 这些平台特有字段，收敛成下面那份统一事件。

目前内置的是 `gitea`。这套接口是稳定的——这是本服务设计成通用转发器的关键。

---

## 配置 —— 所有环境相关值全部外移

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

完整模板见 [`config/webhook2nats.example.toml`](config/webhook2nats.example.toml)，所有值都是占位符，**可以安全地进版本库**；真实配置留在宿主机，不要提交。

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
| `issue_comment` | issue 或 PR 下的评论（含 PR 主对话区） |
| `pull_request` | PR 新建 / 同步 / 关闭… |
| `review` | 评审通过 / 驳回 / 评审意见 / **请求评审** |

按事件类型还会多带这些可选字段：`is_pr`（是否为 PR）、`comment_url`、`mentions`、`review_state`、`reviewers`。

> **`ts` 是接收时刻**，不是事件在平台上的发生时间 —— Gitea 的 webhook payload
> 里没有时间字段。上面示例里的 `"2026-10-10T09:30:00Z"` 就是真实会出现的格式。

## HTTP 契约

**所有响应都是 JSON**，`Content-Type: application/json; charset=utf-8`。

- 成功（`2xx`）给 `status` 字段，失败给 `error` 字段。
- 多数响应带 `delivery`（原样回显请求的 delivery 头）—— 平台侧可以直接拿它
  和日志里的 `delivery=` 对上号。
- 多数错误额外带 `hint`，说明**该改哪里的配置**，不用翻代码。

- `GET /healthz` → NATS 连通返回 `200`（`status` 为 `ok`），否则 `503`（`error` 为 `nats-down`）
- `GET /version` → `200`，`status` 为 `ok`，另返回 git commit SHA 与启动至今的秒数
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

## 安全说明

- 转发器**不持有 Gitea token**，用的 NATS 账号也只能发布三个 subject、不能读消息、不能冒充任何 Agent。即使被打穿，影响面仅限于伪造门铃——而门铃里只有公开元数据和 URL，没有正文。
- webhook 用 HMAC-SHA256 校验；监听端应放在内网 / tailnet，不暴露公网。

### 密钥怎么放：不需要你设任何文件权限

密钥**不放进 bind mount 的 `/config`**，走 [`relay.env`](relay.env.example) + compose 的 `env_file:`：

```
./config/      →  /config   (:ro)   subject、路径、超时、members.json —— 零密钥
./relay.env    →  不进容器            NATS_PASSWORD + WEBHOOK_SECRET
./data/        →  /data              去重状态
```

Docker 在**宿主机侧**读取 `relay.env` 并注入为环境变量——**这个文件从头到尾不进入容器的文件系统**，没有任何途径能把它 mount 进去。所以：

- ❌ **不需要在 DSM 文件管理器里给任何文件单独设 `0600`**。共享文件夹的 ACL 已经在管「其他 DSM 用户能不能看」这件事，那步可以整个省掉。
- ✅ 拿走容器 ≠ 拿走密钥——密钥从来没在容器里。
- ✅ `/config` 是 `:ro`，容器改不了 `members.json`，也就改不了门铃去向。

真正要保护的是**宿主机上那份 `relay.env` 的读权限**（拿到 `WEBHOOK_SECRET` 就能伪造门铃，但门铃里只有公开元数据和 URL）。它的**写**权限由共享文件夹权限兜底。

compose 默认带了 `cap_drop: ALL` 和 `no-new-privileges` —— 这台服务只需要「读请求体 + 出网连 NATS」，不需要任何特权；因为监听的是 8090 高位端口，去掉 `NET_BIND_SERVICE` 也能正常启动。

## 日志

排查门铃问题的第一现场。默认每请求一行：

```
ts=2026-10-11T13:38:57.412+08:00 lvl=INFO provider=gitea delivery=b1f4c9e2…
repo=acme/webhook2nats number=134 kind=issue_comment actor=zhangsan
mentions=[alice wuming] subjects=[evt.gitea.notify.h1.alice]
status=200 decision=accepted dur_ms=1.2 peer=10.0.0.9 msg="请求处理完成"
```

### 按 delivery 把日志和门铃对上号

Agent 收到的 NATS 消息里也有同一个 `delivery`。想确认「这条门铃到底发到哪了」，直接：

```bash
docker logs webhook2nats 2>&1 | grep 'delivery=b1f4c9e2'
```

### 会记什么、不会记什么

> **只记「已经发给 Agent 的字段」。**

`repo` / `number` / `actor` / `mentions` / `subjects` / `delivery` 这些本来就在
NATS 消息里 —— Agent 已经持有了，日志再记一遍**不增加任何暴露面**。

**不进日志**：`comment.body`、签名头、任何 secret。
这三样被格式化层硬性拦掉（`logs.py` 的 `_FORBIDDEN_FIELDS`），不依赖调用方自觉。

`[logging] log_bodies = true` 可以在排查解析 bug 时临时放行正文 —— 那是唯一一处例外。

### 怎么用它定位「门铃没响」

典型现场：某人被 @ 了但没收到门铃。日志里看这两行的差异就够了：

```
mentions=[alice wuming] subjects=[evt.gitea.notify.h1.alice]
```

`mentions` 里有 `wuming`，但 `subjects` 里没有他 —— **`wuming` 不在 `members.json` 里**。
正文记不记都无所谓，元数据已经给出答案。

### 输出与轮转

| 去处 | 上限 | 怎么改 |
|---|---|---|
| stdout（`docker logs`） | 10MB × 3 | compose 里的 `logging.max-size` / `max-file` |
| `/data/logs/relay.log` | 5MB × 5 | `[logging] max_bytes` / `backups`，或留空 `file` 不写文件 |

两边都是有界的，不会无限长。

**日志文件建不出来时不会让服务起不来** —— 自动降级成只写 stdout 并打一条
警告。比如 `/data` 被改成只读挂载时，relay 照常工作，只是没有文件日志。

### 401 为什么是 WARNING 而不是 ERROR

401 是**未认证**请求 —— 任何能连到 8090 的人都能零成本刷。每一刀都记明细
等于给对方一个日志写放大器：填满 5MB 轮转文件，把真正的投递记录挤出去，
攻击者反而达成目的。

所以 401 走限流：**每分钟前 5 条记明细，之后聚合成一行计数**。
保留了「有人在撞门」的检测信号，又不让它无上限占磁盘。

### 排查正则解析 bug

有一种情况元数据救不了：正文里明明有 `@alice`，但正则没抽到。这时
Agent 消息里 `mentions` 是空的，日志里也是空的 —— 两边一样，
分不清是「没提到」还是「提到了没认出来」。

这时把 `[logging] log_bodies` 临时改成 `true`，重启，抓一次，然后关回去。

### JSON 格式

给 log shipper 用：

```toml
[logging]
format = "json"
```

### 不做 `/metrics`

目前没有 Prometheus，`requests_total` 这种数字用日志 grep 就够。
将来真要接，名字锁定为：

```
webhook2nats_requests_total{status}
webhook2nats_publish_duration_seconds
webhook2nats_publish_failures_total{subject}
webhook2nats_deliveries_seen_total
```

名字只是先定下来，**日志行不为了对齐 Prometheus 语法** ——
`status=200` 和 `status="200"` 一样好 grep，没必要为此牺牲可读性。

## 验收时容易踩的两个坑

这两条都让 relay 看起来「坏了」，其实不是。

### 一、Core NATS 不留存消息 —— 订阅者必须先在线

**Core NATS 不留存**。事件发布的那一刻没人在听，消息就永久消散了。

所以验收脚本的正确顺序是：**先起订阅 → 确认在线 → 再回帖触发**。
反过来做（先发事件、再连订阅）必然收不到，而且 relay 会回 `accepted`
—— 因为它**确实**成功发布了，只是当时没人在听。

日志能证明这一点：看到 `subjects=[...] decision=accepted` 就说明
relay 侧完全正常，问题在订阅时机。

### 二、订阅 `evt.>`，不是 `evt.gitea.notify.>`

两个默认 subject 不在同一个命名空间下：

| 用途 | 默认值 |
|---|---|
| 定向门铃 | `evt.gitea.notify.<host>.<id>` |
| 全厅公告 | `evt.gitea.broadcast` |

订阅 `evt.gitea.notify.>` **收不到全厅公告，而且不会有任何报错**。
要覆盖两者请订阅 `evt.>`。

## 测试

四套测试，都不需要真实 NATS 或外网：

```bash
python3 test_deploy.py            # 部署产物：README↔配置模板↔compose 是否漂移
cd relay
python3 test_internals.py         # 配置加载/校验、provider、归一化、@提及、去重、路由
python3 test_relay.py             # HTTP 端到端：真 socket、真 HMAC，只有 NATS 是假的
python3 test_logs.py              # 日志：降级、隐私、限流、轮转、结构性护栏
```

**`relay/fixtures.py` 里的 payload 严格对照 Gitea `modules/structs/hook.go` 的结构体造**，不是凭印象编的 —— 正是那几个结构体里藏着本项目踩过的坑（payload 没有 `action_at`、`ReviewPayload` 用 `content` 而非 `body`、`requested_reviewer` 是顶层单数字段）。

覆盖：HMAC 拒绝与篡改检测、代码块/邮箱/路径不误触、中文紧邻字符 @提及、成员映射路由与坏数据拒绝、公告判定、delivery 去重往返与重启保持、缺 delivery 头拒绝、失败回滚后同 UUID 可重试、inflight 冲突、双事件头映射与优先级、评审三事件、请求评审与推 commit 的区分、body 上限与 Content-Length 防护、端到端 webhook 处理。

`test_deploy.py` 专门盯**文档与产物漂移** —— README 里的 provider 示例与 `config/webhook2nats.example.toml` 不一致、两份 compose 除了镜像来源之外不一致、README 链接指向不存在的文件，这些都会被它挡住。

`test_logs.py` 里每一条都在钉一个**具体的失败模式**，不是凑覆盖率：

| 测试 | 钉住的事 |
|---|---|
| 日志目录不可写 | 服务照常起来，**不能因为日志配置而启动失败** |
| 轮转上限 | 写 500 行之后总大小仍有界 ——「不要无限存」得真的成立 |
| 隐私过滤 | `body` / `secret` / `password` 不出现在任何日志里 |
| 401 级别与限流 | 未认证请求刷不爆日志；且**限流本身被接线到了 401 路径上** |
| AST 护栏 | 每个 `return` 之前都有 `_reply` —— 日志不会漏掉某个分支 |
| 反向自检 | 把上面某条破坏掉，测试**必须失败** —— 防止护栏本身是假的 |

最后一条是这套测试的关键：护栏测试自己也可能是摆设。所以每条结构
性断言都配了一条「故意破坏 → 必须报错」的反向自检。

## 目录结构

```
relay/
  app.py            # 装配：加载配置 → 建注册表 → 连 NATS → 起 HTTP
  config.py         # 配置加载（env > TOML > 默认）+ 启动校验 + 成员映射校验
  providers.py      # provider 注册表 + @提及抽取 + Gitea payload 归一化
  core.py           # 去重 / 收件人解析 / 公告判定（与平台无关）
  logs.py           # 日志：格式化 / 滚动 / 降级 / 限流 / 隐私过滤
  server.py         # HTTP 接收层（不认识任何具体平台）
  fixtures.py       # 真实 Gitea payload fixture
  test_internals.py # 单元测试
  test_relay.py     # HTTP 端到端测试
  test_logs.py      # 日志的降级/隐私/限流护栏
  Dockerfile
config/
  webhook2nats.example.toml   # 配置模板（无环境值，可进库）
  members.json                # 真实成员映射（被 .gitignore 排除）
relay.env.example              # 密钥模板（无环境值，可进库）
docker-compose.yml            # 用预构建镜像
docker-compose.build.yml      # 从源码构建
test_deploy.py                # 部署产物一致性测试
data/                         # 去重状态（被 .gitignore 排除）
```

**哪些能进库、哪些不能**：带 `.example` 后缀的模板、两份 compose、测试 fixture、以及代码本身**都不含环境值**，所以可以公开。真实配置（`relay.env`、`config/` 下的 `webhook2nats.toml` 与 `members.json`）、NATS ACL 片段仍留在你的 NAS 上，由 `.gitignore` 排除。

## 部署

仓库根目录有**两份** compose，按你从哪拿镜像来选：

| 文件 | 镜像来源 | 用法 |
|---|---|---|
| [`docker-compose.yml`](docker-compose.yml) | 拉取预构建镜像（GHCR） | `docker compose up -d` |
| [`docker-compose.build.yml`](docker-compose.build.yml) | 从 `./relay` 本地构建 | `docker compose -f docker-compose.build.yml up -d --build` |

> 做成两份**独立完整文件**而不是 Compose 的 `-f a.yml -f b.yml` 覆盖写法，是因为
> 群晖 Container Manager 一次只认一个 compose 文件。代价是两份有重复，
> 改动时必须同步——`test_deploy.py` 里有一条用例专门盯着这个。

NAS 离线、或还没配 CI 镜像时用 `docker-compose.build.yml`；有 registry 之后用默认那份。

```bash
# 1) 准备两个被忽略的文件
cp relay.env.example relay.env          # 填入 NATS 账号 + WEBHOOK_SECRET + NATS_PASSWORD
#    并在 config/members.json 里填好 登录名 → {host, id} 映射
#    （可选）把 config/webhook2nats.example.toml 复制成 webhook2nats.toml 填好
#    未配置时 CONFIG_FILE 指向的文件不存在，relay 会按纯环境变量模式运行

# 2) 起
docker compose up -d          # 或 docker compose -f docker-compose.build.yml up -d --build

# 3) 确认
curl -s localhost:8090/healthz      # → ok
docker compose logs -f
```

挂载关系（两份一致）：

| 宿主机 | 容器内 | 模式 | 内容 |
|---|---|---|---|
| `./config` | `/config` | `:ro` | `members.json` + `webhook2nats.toml`，**零密钥** |
| `./relay.env` | **不进容器** | `env_file:` | `NATS_PASSWORD`、`WEBHOOK_SECRET` |
| `./data` | `/data` | rw | `seen.json` 去重状态 |

镜像本身不含任何环境值，同一个不可变镜像可以在任何环境跑。

### 镜像 tag 的选择与风险

默认用的是 `:latest`。四种写法的取舍：

| 写法 | 优点 | 风险 |
|---|---|---|
| `:latest`（默认） | 永远拿最新，不用想版本 | **是移动指针**——同一个名字不保证是同一个镜像。NAS 自动更新、重启重建、或将来加了 `pull_policy: always` 时，可能悄悄换成没测过的版本 |
| `:1.0` 小版本 | 可复现、可回滚 | 忘了同步 tag 就会一直用旧版 |
| `:<git-sha>` | 最精确，能追到具体 commit | 不好记，镜像列表会很长 |
| `none` + 手动 `docker tag` | 完全自控 | 每次升级都多一步手工操作 |

**当前这套的风险偏低**：默认的 `docker-compose.yml` 用的是 `image:` 而非 `build:`，
`docker compose up` 不会去拉远端（除非镜像本地不存在或你显式 `pull`）；而
`docker-compose.build.yml` 每次都在本地重新构建，tag 固定为 `webhook2nats:local`，
不存在移动指针。

**真正需要警惕的是将来这两件事**：一是给 compose 加了 `pull_policy: always`；
二是群晖 Container Manager 里配了「自动更新镜像」。任一条成立，`:latest` 就会
开始自动漂移。届时建议改成钉小版本。

## 设计取舍

- **出站 schema 固定，入站适配可配**：那份统一事件 JSON 是**出站契约，定下来就不动**——已经有多个 Agent 在订阅，动一次所有下游都得跟着改，属于破坏性变更。所以 `kind` 保持在四个取值，宁可往里塞也不往外加。入站那头（头名、验签算法、事件映射）各平台差异大且相对稳定，尽量配置化：接新平台应该只是一次 diff，而不是一次发版。
- **平台适配做成声明式配置，而不是运行时插件**：路径、头名、验签算法这些各平台差异很小，写死在代码里每次接新平台都要改代码；声明式配置把它变成改配置。payload 归一化确实各平台结构不同，那部分保留为代码，不硬套进配置。
- **TOML 而不是 .env / YAML**：零新增依赖、重复键报错、口令不会被隐式转型、对 Tab 不敏感——后者对 NAS 上手工编辑配置很关键。
- **不上 JetStream**：Agent 只需要一声即时门铃；离线期间漏掉的，靠来源平台未读状态这条权威对账来源补。持久化队列会重复存储平台已经存的东西。
- **只通知、不行动**：不持平台写权限，不能标已读，不能代替 Agent 发言。
- **配置外置于镜像**：所有环境相关值都挂载/注入，同一个不可变镜像可以在任何环境跑，也才敢公开发布。
