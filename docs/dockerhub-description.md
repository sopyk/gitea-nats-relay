# Docker Hub 镜像说明

> 这段是给 Docker Hub 页面用的文案，**不进镜像**。
> 用的时候到 [Docker Hub 仓库页面](https://hub.docker.com/repository/draft/sopyk/webhook2nats/general)
> 把下面两段分别粘进去即可。

## Short description（概述，限 100 字符）

```
Webhook → Core NATS 实时门铃转发：验签、去重、解析收件人，毫秒级唤醒订阅的 Agent。
```

This is the one-line summary. It is intentionally only Chinese because the box
is small and this deployment's primary audience is Chinese-speaking; if you
prefer English there, swap in:

```
Realtime Webhook → Core NATS bell relay: verify, dedupe, resolve recipients, wake subscribed agents in ms.
```

## Full description（完整说明，Markdown）

The full description below is bilingual by section — each heading has a Chinese
block and an English block. Paste it as-is; Docker Hub renders Markdown.

```markdown
# Webhook2NATS

**轻量的 Webhook → Core NATS 实时门铃转发器。**
A lightweight realtime **webhook → Core NATS** doorbell relay.

平台（Gitea / Forgejo / GitHub / GitLab / 任何会发 Webhook 的系统）推事件，
这个通道负责校验签名、去重、解析「该通知谁」，然后把一个只含元数据的精简事件
发布到 NATS，让在线的 Agent 毫秒级被叫醒——而不是靠轮询定时抓。

A webhook platform fires events at this channel, which verifies the signature,
deduplicates, and resolves who should be notified, then publishes a small
metadata-only event to NATS so a subscribed agent is woken in milliseconds —
instead of polling on a timer.

- 纯 **Core NATS**：无 JetStream、不存消息历史、无数据库
- 镜像零环境值，同一镜像可在任何环境运行
- `linux/amd64` + `linux/arm64` 双架构
- 只做通知：不存正文、不标已读、不代替 Agent 行动

- Pure **Core NATS** — no JetStream, no message history, no database
- Zero environment values baked into the image
- Multi-arch: `linux/amd64` + `linux/arm64`
- Notification only — it never stores bodies or acts on your behalf

## 快速开始 / Quick start

```bash
docker run -d --name webhook2nats \
  -e NATS_SERVERS=nats://<host>:4222 \
  -e NATS_USER=<user> -e NATS_PASSWORD=<pass> \
  -e WEBHOOK_SECRET=<same-secret-as-in-gitea> \
  -e RELAY_ID=<host>.webhook2nats \
  -v /host/config:/config:ro \
  -v /host/data:/data \
  ghcr.io/sopyk/webhook2nats:latest
```

`/config/members.json` 提供「登录名 → {host, id}」映射：```json
{"members": {"alice": {"host": "h1", "id": "alice"}}}
```

## 订阅与验证 / Subscribe & verify

```bash
nats sub 'evt.>'
```

⚠️ **必须先订阅再触发** —— Core NATS 不留存消息，发布那一刻没人在听，
消息就永久消散了，而 relay 仍然会返回 `accepted`（它确实发出去了）。

⚠️ **Subscribe before triggering.** Core NATS keeps no history: if nobody is
listening at the moment of publish, the message is gone for good, while the
relay still answers `accepted` (it really did publish it).

## 文档 / Documentation

完整文档以中文为准，英文版为摘要。
Full documentation is in Chinese; an English summary is provided.

- https://github.com/sopyk/webhook2nats
- 中文 README: `README.md`
- English README: `README.en.md`
```

## 发布时注意

- 这段文案里**不要出现具体版本号**。仓库里有一条测试在盯这件事
  （`test_deploy.py::test_no_stale_version_literals_anywhere`）——
  版本号应当只有 `VERSION` 文件一处，CI 从它推 tag。
- 镜像的 tag 策略：`:latest` 只跟正式版走，预发布（`rc` / `beta` / `alpha`）
  不移动它。CI 有断言在强制，违反则构建失败。
- 如果以后加了 Docker Hub 推送，这里引用的链接要同步检查是否有效。
