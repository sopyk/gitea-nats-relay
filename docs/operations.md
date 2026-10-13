# 运维手册

部署、日志、排障、验收。

### 安全说明

- 转发器**不持有 Gitea token**，用的 NATS 账号也只能发布三个 subject、不能读消息、不能冒充任何 Agent。即使被打穿，影响面仅限于伪造门铃——而门铃里只有公开元数据和 URL，没有正文。
- webhook 用 HMAC-SHA256 校验；监听端应放在内网 / tailnet，不暴露公网。

### 密钥怎么放：不需要你设任何文件权限

密钥**不放进 bind mount 的 `/config`**，走 [`relay.env`](../relay.env.example) + compose 的 `env_file:`：

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

### 日志

排查门铃问题的第一现场。默认每请求一行：

```
ts=2026-10-11T13:38:57.412+08:00 lvl=INFO provider=gitea delivery=b1f4c9e2…
repo=acme/webhook2nats number=134 kind=issue_comment actor=zhangsan
mentions=[alice wendy] subjects=[evt.gitea.notify.h1.alice]
status=200 decision=accepted dur_ms=1.2 peer=192.0.2.9 msg="请求处理完成"
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
mentions=[alice wendy] subjects=[evt.gitea.notify.h1.alice]
```

`mentions` 里有 `wendy`，但 `subjects` 里没有他 —— **`wendy` 不在 `members.json` 里**。
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

> ⚠️ **它记的是完整的原始 payload，不只是评论正文。**
> 解析 bug 可能出在 `sender` / `issue` / 任意一层，只记一段反而会让人
> 误以为是别的问题 —— 但代价是**日志里会出现用户提交的全部内容**。
> 上限 8000 字符，超出会标出被截了多少。
> 里面的值仍然经过控制字符转义，所以正文里的换行不会把日志劈成两行。

`log_bodies` 只放行正文类字段（`body` / `comment_body` / `review_content`）。
**密钥类字段（`secret` / `password` / `signature` / `token` / …）没有任何开关**。

### JSON 格式

给 log shipper 用：

```toml
[logging]
format = "json"
```

### NATS 抖动期间 relay 会回 200 —— 这是有意的

实测确认（`test_e2e.PublishFailureTest`）：

| 时刻 | relay 行为 |
|---|---|
| NATS 停机期间投递 | 回 `200 accepted`，消息进重连队列 |
| NATS 恢复后 | **消息被补发**，订阅端确实收到 |
| relay 进程在断连期间重启 | 缓冲区随进程消失，消息丢失 |

所以 `200` 在这里是诚实的：平台不会重试，relay 自己保证补发。

**代价**：宁可重复也不丢（`max_reconnect_attempts=-1` + 缓冲）。
反过来如果停机期间报 502，平台会重试，叠加补发就是重复门铃 —— 那更糟。

**唯一真实的丢失窗口**是最后一行：NATS 断着的时候重启 relay。
排查「某条门铃彻底没出现」时，先看 relay 的启动时间是不是在那次抖动里。

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

### 验收时容易踩的两个坑

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

### 测试

四套测试，都不需要真实 NATS 或外网：

```bash
python3 test_deploy.py            # 部署产物：README↔配置模板↔compose 是否漂移
cd relay
python3 test_internals.py         # 配置加载/校验、provider、归一化、@提及、去重、路由
python3 test_relay.py             # HTTP 端到端：真 socket、真 HMAC，只有 NATS 是假的
python3 test_logs.py              # 日志：降级、隐私、限流、轮转、结构性护栏
python3 test_e2e.py               # 仿真：keep-alive / 注入 / 并发 / 真实 NATS 全链路
```

> `test_e2e.py` 需要 docker 或 `W2N_NATS`（会自动拉起一个 nats 容器）。
> 起不来时那几组会 **skip 并说明原因**，不是静默通过。
> 它内置的订阅端是**手写的裸 socket**（不依赖 nats-py），所以验的是
> 线上真实协议字节 —— 顺带证明 relay 的输出对「不用 nats-py 的订阅者」也成立。

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

`test_e2e.py` 打的是**真 socket**，因为下面这些问题伪造 Handler 实例
根本测不出来 —— 全都是实打实撞上才发现的：

| 测试 | 钉住的事 |
|---|---|
| keep-alive 字段污染 | 同一条连接上的请求**不共用日志字段**（实测：401 行会挂上一个合法请求的仓库和作者，404 行会带上 `provider=gitea`） |
| 日志注入 | payload 字段 / 正文里的换行**不能伪造出一条日志**；ANSI 转义被中和 |
| 堆栈可见 | text 是默认格式，它**必须**输出堆栈；且堆栈行带 `delivery`（否则按 delivery 查堆栈查不到） |
| 并发 | 40 个并发请求 → 40 条干净日志，字段不互串 |
| 真实 NATS | **消息真的到达订阅端**，subject 与内容都对；日志的 delivery 与消息的对得上 |
| NATS 抖动 | 停机期间回 200，恢复后**消息真的补发**（这是实测结论，见上文） |


### 部署

仓库根目录有**两份** compose，按你从哪拿镜像来选：

| 文件 | 镜像来源 | 用法 |
|---|---|---|
| [`docker-compose.yml`](../docker-compose.yml) | 拉取预构建镜像（GHCR） | `docker compose up -d` |
| [`docker-compose.build.yml`](../docker-compose.build.yml) | 从 `./relay` 本地构建 | `docker compose -f docker-compose.build.yml up -d --build` |

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

### ⚠️ 单实例约束（硬约束，不是建议）

**一个 Gitea webhook 回调只能指向一个 relay 实例。**

去重状态（`seen.json`）存在**每个实例本地**的卷上。如果把同一个回调地址
指向多个实例，同一个 delivery 会在每个实例上各处理一次：

- 门铃**重复下发**，Agent 被叫醒两次
- **每一侧都记 `decision=accepted`，没有任何报错**
- 从任何一侧的日志里都看不出异常 —— 两台机器各自都"成功"了

这个失败模式特别难发现，因为它在每一侧看起来都是对的。横向扩展之前
必须先把去重换成**共享后端**（JetStream / KV / Redis 之类）。

> 同一台 relay 前面挂反代、或者 Gitea 那边只配了一个回调地址指向反代，
> 都不算多实例 —— 去重仍然只有一份。反代把请求转给同一台机器是安全的。

### 镜像 tag 的选择与风险

默认用的是 `:latest`。四种写法的取舍：

| 写法 | 优点 | 风险 |
|---|---|---|
| `:latest`（默认） | 不用想版本；**只跟正式版走，不会拉到测试版** | **是移动指针**——同一个名字不保证是同一个镜像。NAS 自动更新、重启重建、或将来加了 `pull_policy: always` 时，可能悄悄换成没测过的版本 |
| `:1.0` 小版本 | 可复现、可回滚 | 忘了同步 tag 就会一直用旧版，而且**每次发版都要记得改 compose** |
| `:<git-sha>` | 最精确，能追到具体 commit | 不好记，镜像列表会很长 |
| `none` + 手动 `docker tag` | 完全自控 | 每次升级都多一步手工操作 |

**为什么默认选 `:latest` 而不是钉版本号：**

「版本号出现在多个地方、有的改了有的没改」是发版事故里最常见也最难查的
一种 —— 各处看起来都像对的，只有对照着比才能发现有一个是旧的。所以这个
项目**把版本号收敛成一处**（仓库根的 `VERSION` 文件），CI 从它注入构建，
compose 里一个版本号都不写。

代价是失去可复现性，这一条我们不粉饰：如果你要「同一份配置在任何时候
拉到的都是同一个镜像」，就该把 compose 的 image 改成 `:1.0`（或别的钉法），
并接受发版时多一步手工更新。

**`:latest` 不会拿到测试版。** CI 里有断言强制：预发布（`rc` / `beta` /
`alpha`）构建若产出 `latest`，构建直接失败。所以 `docker compose pull`
拿到的永远是已发布的稳定版。

**当前这套的风险偏低**：默认的 `docker-compose.yml` 用的是 `image:` 而非 `build:`，
`docker compose up` 不会去拉远端（除非镜像本地不存在或你显式 `pull`）；而
`docker-compose.build.yml` 每次都在本地重新构建，tag 固定为 `webhook2nats:local`，
不存在移动指针。

**真正需要警惕的是将来这两件事**：一是给 compose 加了 `pull_policy: always`；
二是群晖 Container Manager 里配了「自动更新镜像」。任一条成立，`:latest` 就会
开始自动漂移。届时建议改成钉小版本。

