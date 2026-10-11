"""HTTP 接收层。

这一层只负责：把请求交给对应的 provider、读 body、验签、去重、调用核心、
把结果翻译成状态码。它不认识 Gitea，也不认识任何具体平台 —— 平台知识全在
providers.py 和配置里。
"""
import concurrent.futures
import json
import logging
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import logs as logs_mod

logger = logs_mod.get_logger()

# 401 限流。每分钟前 5 条记明细，之后聚合计数。
#
# 为什么不用 ERROR：401 是**未认证**请求，任何能连到端口的人都能零成本刷。
# 每一刀都记明细等于给对方一个日志写放大器 —— 填满 5MB 轮转文件，把真正
# 的投递记录挤出去，攻击者反而达成目的。聚合计数保留了「有人在试」这个
# 检测信号，又不让它无上限占磁盘。
#
# 做成 class 属性（而不是模块全局）有两个原因：
#   1. 每个进程/每个 App 实例各有一份，不会互相偷配额
#   2. 测试可以给实例塞一个自己的限流器，不必重置模块全局状态
_bad_sig = logs_mod.RateLimiter(limit=5, window=60.0, aggregate_every=25)


def log_stack(where="", **fields):
    """记录未预期异常 —— 位置保留在 server.py，一眼能看出是哪一层炸的。

    `fields` 由调用方（Handler._stack）传入本请求已累积的元数据，让堆栈
    能按 delivery 与请求关联上。
    """
    logs_mod.event(logger, logging.ERROR, "未处理的异常",
                   layer=where or "server", exc_info=True, **fields)


# 进程启动时刻，供 /version 报 uptime 用。
_STARTED_AT = time.time()


def version_info():
    """版本信息。

    Git SHA 由构建时注入（Dockerfile 的 ARG GIT_SHA → CI 传 --build-arg）。
    本地跑时拿不到就是 unknown —— 不能为了填这个字段去依赖 .git 目录，
    交付镜像里本来就没有。
    """
    return {
        "version": os.environ.get("WEBHOOK2NATS_VERSION", "") or "unknown",
        "started_at": _STARTED_AT,
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
    }

# 请求体上限。验签必须对完整 body 做，所以 body 得先读完 —— 没设上限的话，
# 一个未认证的客户端就能靠超大 Content-Length 把进程内存吃光（实测 10MB
# 无签名请求会被完整读入才回 401）。Gitea 的 webhook payload 实测在几十 KB 量级，
# 1MB 已经很宽松。
MAX_BODY_BYTES = 1 * 1024 * 1024

# 提前返回 4xx 时最多读掉多少字节的残余请求体。超过就直接关连接，
# 不为一个注定失败的请求花掉读大 body 的时间。
DRAIN_LIMIT = 64 * 1024

# 单个连接的最长存活时间。读一个几 MB 的 webhook 10 秒绰绰有余；
# 超时的连接会被 close，线程随之释放。
REQUEST_TIMEOUT_SECONDS = 10


class App:
    """把配置、provider、核心串起来。测试可以注入假的 publish。"""

    def __init__(self, cfg, registry, members, seen, relay):
        self.cfg = cfg
        self.registry = registry
        self.members = members
        self.seen = seen
        self.relay = relay
        # 每个实例一份限流器，不共用模块全局 —— 否则测试之间会互相
        # 偷配额，跑得多的那个测试会让后面的测试一条 401 都记不上。
        self.bad_sig_limiter = logs_mod.RateLimiter(
            limit=5, window=60.0, aggregate_every=25)

    def healthz(self):
        return bool(self.relay.nc is not None and self.relay.nc.is_connected)


class Handler(BaseHTTPRequestHandler):
    server_version = "webhook2nats/1.0"
    protocol_version = "HTTP/1.1"
    app = None  # 由 serve() 注入

    def setup(self):
        super().setup()
        # _started / _fields 是**每请求**状态，不是每连接状态。
        # setup() 只在建立连接时调用一次，而 HTTP/1.1 keep-alive 下同一个
        # 实例会循环处理多个请求（handle → handle_one_request 循环）。
        # 所以这里只是给个初值，真正的重置在 _begin_request()。
        self._started = None
        self._fields = {}

    def _begin_request(self):
        """每个请求的入口都必须调它，清掉上一个请求留下的现场。

        不清会怎样（实测过，同一条 TCP 连接上「正常投递 → 401 → 404」）：

          delivery=attack-1 ... repo=SECRET-REPO number=999 actor=secret-actor
          mentions=[alice] status=401 decision=bad-signature

        那是一条**未通过验签**的请求，日志却挂着上一个合法请求的仓库、
        作者和 PR 号。更糟的是 404 那行会带上 `provider=gitea` 和上一个
        请求的 event/kind —— 而 404 意味着压根没匹配到 provider。

        根因是 _mark() 会跳过 None/""（避免打一堆空字段），所以提前返回的
        分支不会覆盖掉全部字段，残留就留到了下一次请求。
        """
        self._started = time.monotonic()
        self._fields = {}

    def _mark(self, **fields):
        """往本次请求的日志字段里塞东西。只在本地累积，不立刻落盘。

        跳过 None / "" 是为了不打一堆空字段；代价是**提前返回的分支
        不会覆盖掉全部字段** —— 这正是 _begin_request 必须清空字典的
        原因，否则残留会串到下一个请求。
        """
        self._fields.update({k: v for k, v in fields.items() if v not in (None, "")})

    def _stack(self, where):
        """记录未预期异常，**并带上本请求已累积的字段**。

        堆栈必须是可关联的：整个日志功能的承诺是「按 delivery 查」，而
        排障时最想要的那一坨堆栈如果只有一句 `layer=_process`，跟这次
        请求是什么事件、哪个仓库完全对不上，等于没有。

        这里刻意复用 self._fields 而不是只补一个 delivery —— 出错时
        通常已经 normalize 过了，repo/actor/mentions 都在里面。

        注意层号必须走 logs.event 的 extra，**不能**写成
        `logger.exception(msg, layer=where)`：那样 layer 会被当成
        logging 的关键字参数，Logger._log() 不认，会抛 TypeError。
        而这行代码在异常处理器里，处理器自己一抛，客户端拿到的是
        连接重置而不是本该给它的 500。
        """
        log_stack(where, **dict(self._fields))

    def _log_request(self, code, decision, extra):
        """把一次请求记成一行。

        401 单独走限流分支 —— 见 _bad_sig 处的说明。
        """
        ctx = dict(self._fields)
        ctx.update(extra)
        started, self._started = self._started, None
        if started is not None:
            ctx["dur_ms"] = round((time.monotonic() - started) * 1000, 1)
        ctx["status"] = code
        ctx["decision"] = decision
        ctx.setdefault("peer", self.client_address[0] if self.client_address else "-")

        if code == 401:
            limiter = getattr(self.app, "bad_sig_limiter", None) or _bad_sig
            if limiter.allow():
                logs_mod.event(logger, logging.WARNING, "验签失败", **ctx)
                return
            n = limiter.take_aggregate()
            if n:
                # 聚合行：保留「有人在撞门」的信号，但只占一行。
                logs_mod.event(logger, logging.WARNING,
                               "验签失败（已限流，明细省略）", suppressed=n, **ctx)
            return

        level = logging.INFO
        if code >= 500 or code in (502, 504):
            level = logging.ERROR
        elif code >= 400:
            level = logging.WARNING
        logs_mod.event(logger, level, "请求处理完成", **ctx)

    def _reply(self, code, msg="ok", decision=None, delivery=None, **extra):
        """回响应，并在**这一处**落本次请求的日志。

        两个理由让日志必须集中在这里，而不是散在 11 个返回分支里：

        1. **不会漏。** 每个分支各有各的早退路径，散着写迟早漏掉一个，
           而漏掉的恰恰会是最该记的那个（`200 accepted`）。
        2. **不会破坏去重生命周期。** 所有 `seen.commit()` / `rollback()`
           都在 `_reply` 之前。日志若插在 begin() 与 commit/rollback 之间，
           一旦抛异常就会跳过 rollback，那个 delivery 会**永久卡在
           inflight** —— 之后平台每次重试都只拿到误导性的 409。这是本项目
           最阴的失败模式，比没日志糟糕得多。
        """
        ok = 200 <= code < 300
        payload = {}
        payload["status" if ok else "error"] = decision or msg
        if delivery:
            payload["delivery"] = delivery
        payload.update(extra)
        body = json.dumps(payload, ensure_ascii=False).encode()

        self._log_request(code, decision or msg, {"delivery": delivery, **extra})

        self.send_response(code)
        # 必须严格声明 application/json：delivery 是回显的用户输入，
        # 声明成 text/html 就可能被浏览器当页面渲染，等于开一个 XSS 口子。
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静默默认 access log —— 我们自己记，更全
        pass

    def _route(self):
        """请求路径（去掉 query string）。

        直接拿 self.path 匹配会让 `POST /gitea-hook?foo=1` 变成 404 ——
        路径是按 provider 配的，本就该按 URL 的 path 部分匹配。
        """
        return urlsplit(self.path).path or "/"

    def do_GET(self):
        self._begin_request()
        route = self._route()
        if route == "/healthz":
            ok = self.app.healthz()
            self._mark(path=route)
            self._reply(200 if ok else 503, "ok" if ok else "nats-down")
        elif route == "/version":
            self._mark(path=route)
            self._reply(200, "ok", **version_info())
        else:
            self._drain()
            self._mark(path=route)
            self._reply(404, "not-found")

    def _drain(self, hard_close_over=None):
        """把请求体从 socket 里读干净，否则 keep-alive 下残余字节会被当成
        下一个请求行去解析 —— 表现为莫名其妙的 400 加连接关闭。

        hard_close_over：超过这个大小就不真读了，直接关连接。超大 body 读它
        没有意义（客户端可能正在慢慢发），而且留着连接等一个必然失败的请求
        白占一个线程。
        """
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            # Content-Length 非法：客户端声称要发的字节数根本不可知，
            # 无法确定还剩多少没读。这条连接不能复用，只能关掉。
            self.close_connection = True
            return
        if n <= 0:
            return  # 本来就没有 body，连接可继续复用
        limit = DRAIN_LIMIT if hard_close_over is None else hard_close_over
        if n > limit:
            # 已经回了 4xx 且读不完：这条连接已经没救了，必须关掉。
            # 但要先回响应，否则客户端只看到 RST 而不是 413。
            self.close_connection = True
            return
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_body(self):
        """返回 (body, error)。error 非 None 时调用方直接回对应状态码。

        旧实现直接 `int(self.headers.get("Content-Length"))`，一个非数字的
        Content-Length 会抛 ValueError 逃出 handler，导致连接被重置、
        客户端收不到任何 HTTP 响应。
        """
        raw = self.headers.get("Content-Length", "0")
        try:
            length = int(raw)
        except (TypeError, ValueError):
            return None, "bad-length"
        if length < 0:
            return None, "bad-length"
        if length > MAX_BODY_BYTES:
            return None, "too-large"
        return (self.rfile.read(length) if length else b""), None

    def do_POST(self):
        self._begin_request()
        route = self._route()
        provider = self.app.registry.get(route)
        self._mark(path=route)
        if provider is None:
            # 路径没匹配上，但请求体可能还在 socket 上 —— 不 drain 的话
            # keep-alive 下残余字节会被当成下一个请求行。
            self._drain()
            self._reply(404, "not-found")
            return
        try:
            self._handle(provider)
        except Exception:  # noqa
            # 兜底：任何漏网的异常都在这里变成一个诚实的 500，而不是
            # 异常逃出去 → 客户端连接被重置、看不到任何状态码。
            self._stack("do_POST")
            self._reply(500, "internal-error")

    def _handle(self, provider):
        self._mark(provider=provider.name)
        body, err = self._read_body()
        if err == "bad-length":
            self._drain()
            self._reply(400, "bad-length",
                        hint="Content-Length 缺失或不是整数")
            return
        if err == "too-large":
            # 顺序很重要：先回 413 再关连接。反过来的话客户端只看到
            # RST/连接重置，拿不到状态码，也无从知道是超限而不是网络问题。
            # body 读不完（可能比上限大得多）就不 drain，直接标记关连接。
            self._reply(413, "payload-too-large",
                        limit_bytes=MAX_BODY_BYTES)
            self._drain()
            return

        delivery = provider.read_delivery(self.headers)
        if delivery:
            self._mark(delivery=delivery)

        if not provider.verify(body, self.headers):
            # body 已经读完了，不用 drain
            self._reply(401, "bad-signature", delivery=delivery,
                        hint=f"核对 provider {provider.name} 的 signature.secret "
                             f"与签名算法")
            return

        state = self.app.seen.begin(delivery)
        if state == "seen":
            self._reply(200, "duplicate", delivery=delivery)
            return
        if state == "inflight":
            self._reply(409, "inflight", delivery=delivery)
            return
        if state == "missing":
            # 没有 delivery 头就完全无法幂等。放行等于每次重投都重复叫醒，
            # 而平台的重试是常态 —— 所以明确拒绝，让运维把 Webhook 配置补全。
            self._reply(400, "missing-delivery", delivery=delivery,
                        hint="平台必须带 delivery 头，否则无法去重")
            return

        try:
            self._process(provider, body, delivery)
        except Exception:  # noqa
            # 这一段必须 rollback：占位不释放的话，同一个 delivery
            # 会被永久卡在 inflight，平台每次重试都只拿到一个误导性的
            # 409（"上一次还在处理中"，实际上早就不处理了），直到进程重启。
            self.app.seen.rollback(delivery)
            self._stack("_process")
            self._reply(500, "publish-error", delivery=delivery)

    def _process(self, provider, body, delivery):
        # 逃生开关打开时，把**完整原始 payload** 记进 body 字段。
        #
        # 记整个 payload 而不是只记 comment.body：解析 bug 可能出在
        # sender / issue / 任意一层，只记一段会让人以为是别的问题。
        # 代价是它含**全部**用户提交的内容（不止评论正文），所以默认关，
        # 且文档里必须把这一点讲清楚 —— 说成「记评论正文」是误导。
        #
        # 见 logs._BODY_FIELDS —— 即使这里传了，默认配置下也会被剥掉。
        if self.app.cfg.logging.log_bodies:
            self._mark(body=body.decode("utf-8", "replace"))

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:  # noqa
            self.app.seen.rollback(delivery)
            self._reply(400, "bad-json", delivery=delivery,
                        hint="请求体不是合法 JSON")
            return
        if not isinstance(payload, dict):
            self.app.seen.rollback(delivery)
            self._reply(400, "bad-json", delivery=delivery,
                        hint="请求体必须是 JSON 对象")
            return

        kind = provider.resolve_kind(self.headers)
        self._mark(event=provider.read_event(self.headers),
                   kind=kind or "-")
        if not kind:
            # 不认识的事件类型：正常地标记为已处理，避免平台无意义重试，
            # 但绝不发布。
            self.app.seen.commit(delivery)
            self._reply(200, "ignored", delivery=delivery)
            return

        # 归一化也在这里面 —— 它对 payload 结构的意外变化零容忍，
        # 而 payload 是平台给的，不能假设它永远是文档里的形状。
        ev = provider.normalize(payload, kind, payload.get("action") or "")
        ev["delivery"] = delivery

        # 只记**已经发给 Agent 的字段** —— 这条隐私规则的完整含义见 logs.py：
        # 这些字段本来就在 NATS 消息里，Agent 已持有，日志再记不增加暴露面。
        # 注释正文、签名头一律不进日志。
        self._mark(
            repo=ev.get("repo"), number=ev.get("number"),
            actor=ev.get("actor"), action=ev.get("action"),
            mentions=ev.get("mentions"), reviewers=ev.get("reviewers"),
            review_state=ev.get("review_state"),
        )

        future = self.app.relay.submit_event(ev)
        try:
            results = future.result(timeout=self.app.cfg.publish_timeout_seconds)
        except concurrent.futures.TimeoutError:
            # cancel() 对已在运行的协程无效，但必须调：run_coroutine_threadsafe
            # 包装的协程还没被调度时它是能被取消的，省掉一次必然重复的门铃。
            future.cancel()
            self.app.seen.rollback(delivery)
            self._reply(504, "publish-timeout", delivery=delivery,
                        hint="NATS 在超时预算内没有确认；平台会重试")
            return

        subjects = [s for s, _ in results]
        self._mark(subjects=subjects or "[]")
        failed = [s for s, ok in results if not ok]
        if failed:
            # 回滚：让平台用同一个 UUID 重试，而不是当成已处理丢弃。
            self.app.seen.rollback(delivery)
            self._reply(502, "publish-failed", delivery=delivery,
                        subjects=subjects, failed=failed)
            return

        self.app.seen.commit(delivery)
        self._reply(200, "accepted", delivery=delivery, subjects=subjects)


def serve(app, listen):
    """建 HTTP server。

    app 通过动态子类绑定到 handler，而不是写成 Handler 的类属性 ——
    类属性是**全局可变状态**，一个进程只能有一个 App，测试里想并行跑两个
    实例就会互相串味。
    """
    host, _, port = listen.rpartition(":")
    host = (host or "0.0.0.0").strip("[]")  # 去掉 IPv6 的方括号

    bound = type("BoundHandler", (Handler,), {
        "app": app,
        # per-connection 超时。BaseHTTPRequestHandler 默认是 None（永不超时），
        # 一个「声明 Content-Length 然后不发数据」的未认证客户端就能永久
        # 占住一个线程 —— ThreadingHTTPServer 不限线程数，等于自己把自己打死。
        # 注意 httpd.timeout 管的是 accept()，对这里没用（那是另一回事）。
        "timeout": REQUEST_TIMEOUT_SECONDS,
    })

    if ":" in host:  # IPv6 字面量
        class V6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        httpd = V6Server((host, int(port)), bound)
    else:
        httpd = ThreadingHTTPServer((host, int(port)), bound)

    httpd.daemon_threads = True
    return httpd