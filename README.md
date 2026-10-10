# gitea-nats-relay

Gitea webhook → Core NATS 的实时"门铃"转发器。接收 Gitea webhook（HMAC 验签、delivery 去重、解析被 @/指派成员），把精简事件发布到 NATS，供各 Agent 秒级被叫醒；离线漏事件由低频 Gitea 巡厅兜底。无 JetStream、不持久化正文。

## 镜像

GitHub Actions 在打 tag `relay-v*` 时构建并推送：

```
ghcr.io/sopyk/gitea-nats-relay:<tag>
```

- 构建上下文：`relay/`（本目录）
- 平台：linux/amd64
- 镜像只含代码与依赖，**不含身份映射、不含任何口令**

## 配置（容器环境变量）

| 变量 | 说明 |
|---|---|
| `NATS_SERVERS` | 如 `nats://<nats-host>:4222` |
| `NATS_USER` / `NATS_PASSWORD` | relay 专用 NATS 账号（仅可发布门铃/广播 subject） |
| `WEBHOOK_SECRET` | 与 Gitea webhook 同一个 HMAC 密钥 |
| `MEMBERS_FILE` | 成员映射 JSON，默认 `/config/members.json`（**宿主只读挂载，不烤进镜像**） |
| `DEDUP_FILE` | 去重状态，默认 `/data/seen.json` |
| `LISTEN` | 默认 `0.0.0.0:8090` |
| `PUBLISH_TIMEOUT_SECONDS` | 等待 NATS 发布确认，默认 3 |

挂载：`./config:/config:ro`（members.json）、`./data:/data`（seen.json）。

## Subject 契约

- `evt.gitea.notify.<host>.<id>`：定向通知（仅 relay 账号可发布，本人可订）
- `evt.gitea.broadcast`：公告/规则类全厅
- `hb.199.gitea-relay`：心跳

## 测试

```bash
cd relay && python3 test_relay.py
```

> 成员映射（members.json）与部署 compose 属内部配置，保存在内网仓库，不在此公开仓库。
