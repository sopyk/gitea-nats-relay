# 接入一个平台

本文讲**怎么加一个新的 Webhook 来源平台**，以及配置里那些「写错了不会报错」的坑。

### 通用模型：接入一个平台要做哪几件事

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

Gitea 走 JSON Webhook，验签用 `X-Gitea-Signature` 放 HMAC-SHA256 十六进制串，delivery ID 在 `X-Gitea-Delivery`。完整配置见 [`config/webhook2nats.example.toml`](../config/webhook2nats.example.toml)：

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

