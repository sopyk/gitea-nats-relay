# 更新日志

[English](CHANGELOG.en.md) · 中文

本项目的版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

本文件记录**面向使用者**的变更。仓库的**唯一版本真源**是根目录的
[`VERSION`](./VERSION) 文件 —— CI 从它注入构建，测试会断言本文件顶部的
版本与它一致。版本号不在别处硬编码，就是为了杜绝「多处出现、有的改了
有的没改」。

---

## 1.0.0

第一个正式版。

### 破坏性变更

- **响应体从纯文本改为 JSON。** 之前 `POST` 的响应体是 `accepted` /
  `duplicate` 这类裸字符串，现在统一是 JSON：

  ```json
  {"status": "accepted", "delivery": "<uuid>", "subjects": ["evt.gitea.notify.h1.alice"]}
  ```

  `Content-Type` 一并改为 `application/json; charset=utf-8`。

  **迁移**：任何形如 `if body == "accepted"` 的判断会**静默失效** ——
  拿到的是字典不是字符串，比较永远为假，不会报错。改成解析字段：

  ```python
  doc = response.json()
  ok = doc.get("status") == "accepted"
  # 失败时是 doc.get("error")，多数还带 doc.get("hint") 说明该改哪里的配置
  ```

  `GET /healthz` 同样受影响：`ok` → `{"status": "ok"}`，
  `nats-down` → `{"error": "nats-down"}`。

### 新增

- **`GET /version`** —— 返回发布版本号（`version`）、精确 commit（`commit`）、
  启动时刻与 uptime。用于确认「我跑的是哪一版」。
- **`[logging]` 配置段** —— 结构化日志，默认每请求一行，带 `delivery` /
  `repo#number` / `mentions` / `subjects` / `status` / `dur_ms`，
  可按 `delivery` 与 NATS 消息精确对上号。整段可省略，省略时等价于
  纯 stdout、info 级别、text 格式、不记正文。详见
  [运维手册](docs/operations.md#日志)。
- **subject 前缀启动校验** —— `notify_subject_prefix` / `broadcast_subject`
  含 NATS 通配符、或公告 subject 落在定向前缀之下时，**启动即报错**。
  这两种配置都会造成「门铃投错人且全程无报错」。
- **配置项的更严格校验** —— 类型错、数值为 0、未知键一律拒绝启动，
  不再静默按默认值跑。

### 修复

- **去重状态文件类型校验** —— 内容是合法 JSON 但类型不对时，
  明确报错而不是静默按空状态重建。
- **成员映射逐条校验** —— 一个非 dict 条目会让**整个文件**被拒绝并指出
  是哪个登录名，而不是让每一次 webhook 抛异常。
- **发布超时的预算关系** —— NATS 侧预算被设为低于 HTTP 层，
  让 NATS 先于 HTTP 层放弃，缩小重复投递窗口。
- **`@提及` 解析** —— 中文紧邻字符也能匹配；代码块、行内码、邮箱、路径
  里的 `@` 不再误触。

### 已知限制

- **不保证「恰好一次投递」。** Core NATS 无 ack 机制，超时窗口无法完全消除。
  去重保证的是「不重复处理」，下游 Agent 若需严格幂等请用 `delivery` 字段。
- **离线期间的门铃会丢。** 这是 Core NATS 的设计，由另一条低频巡厅兜底对账。
- **单个 relay 实例对应单个 webhook 回调地址。** 去重状态是本地的，
  多实例会让同一 delivery 重复下发且无任何报错。详见
  [运维手册](docs/operations.md#部署)。