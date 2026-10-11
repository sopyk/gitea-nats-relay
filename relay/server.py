"""HTTP 接收层。

这一层只负责：把请求交给对应的 provider、读 body、验签、去重、调用核心、
把结果翻译成状态码。它不认识 Gitea，也不认识任何具体平台 —— 平台知识全在
providers.py 和配置里。
"""
import concurrent.futures
import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log_stack():
    """统一记录未预期异常 —— 位置保留在 server.py，一眼能看出是哪一层炸的。"""
    print("[relay] unhandled:", traceback.format_exc().strip(), flush=True)

# 请求体上限。验签必须对完整 body 做，所以 body 得先读完 —— 没设上限的话，
# 一个未认证的客户端就能靠超大 Content-Length 把进程内存吃光（实测 10MB
# 无签名请求会被完整读入才回 401）。Gitea 的 webhook payload 实测在几十 KB 量级，
# 1MB 已经很宽松。
MAX_BODY_BYTES = 1 * 1024 * 1024

# 提前返回 4xx 时最多读掉多少字节的残余请求体。超过就直接关连接，
# 不为一个注定失败的请求花掉读大 body 的时间。
DRAIN_LIMIT = 64 * 1024


class App:
    """把配置、provider、核心串起来。测试可以注入假的 publish。"""

    def __init__(self, cfg, registry, members, seen, relay):
        self.cfg = cfg
        self.registry = registry
        self.members = members
        self.seen = seen
        self.relay = relay

    def healthz(self):
        return bool(self.relay.nc is not None and self.relay.nc.is_connected)


class Handler(BaseHTTPRequestHandler):
    server_version = "webhook2nats/1.0"
    protocol_version = "HTTP/1.1"
    app = None  # 由 serve() 注入

    def _reply(self, code, msg="ok"):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静默默认 access log
        pass

    def do_GET(self):
        if self.path == "/healthz":
            ok = self.app.healthz()
            self._reply(200 if ok else 503, "ok" if ok else "nats-down")
        else:
            self._reply(404, "not-found")

    def _drain(self):
        """把请求体从 socket 里读干净。

        keep-alive 下如果直接回响应就返回，剩下的 body 字节会被当成下一个请求行
        去解析 —— 表现为莫名其妙的 400 加连接关闭。
        """
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            return
        # 超大 body 没有必要真读（客户端可能正在慢慢发），关连接即可，
        # 所以这里限量并把连接标记为不再复用。
        if n > DRAIN_LIMIT:
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
        provider = self.app.registry.get(self.path)
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
            log_stack()
            self._reply(500, "internal-error")

    def _handle(self, provider):
        body, err = self._read_body()
        if err == "bad-length":
            self._drain()
            self._reply(400, "bad-length")
            return
        if err == "too-large":
            self._drain()
            self._reply(413, "payload-too-large")
            return

        if not provider.verify(body, self.headers):
            # body 已经读完了，不用 drain
            self._reply(401, "bad-signature")
            return

        delivery = provider.read_delivery(self.headers)
        state = self.app.seen.begin(delivery)
        if state == "seen":
            self._reply(200, "duplicate")
            return
        if state == "inflight":
            self._reply(409, "inflight")
            return
        if state == "missing":
            # 没有 delivery 头就完全无法幂等。放行等于每次重投都重复叫醒，
            # 而平台的重试是常态 —— 所以明确拒绝，让运维把 Webhook 配置补全。
            self._reply(400, "missing-delivery")
            return

        try:
            self._process(provider, body, delivery)
        except Exception:  # noqa
            # 这一段必须 rollback：占位不释放的话，同一个 delivery
            # 会被永久卡在 inflight，平台每次重试都只拿到一个误导性的
            # 409（"上一次还在处理中"，实际上早就不处理了），直到进程重启。
            self.app.seen.rollback(delivery)
            log_stack()
            self._reply(500, "publish-error")

    def _process(self, provider, body, delivery):
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:  # noqa
            self.app.seen.rollback(delivery)
            self._reply(400, "bad-json")
            return
        if not isinstance(payload, dict):
            self.app.seen.rollback(delivery)
            self._reply(400, "bad-json")
            return

        kind = provider.resolve_kind(self.headers)
        if not kind:
            # 不认识的事件类型：正常地标记为已处理，避免平台无意义重试，
            # 但绝不发布。
            self.app.seen.commit(delivery)
            self._reply(200, "ignored")
            return

        # 归一化也在这里面 —— 它对 payload 结构的意外变化零容忍，
        # 而 payload 是平台给的，不能假设它永远是文档里的形状。
        ev = provider.normalize(payload, kind, payload.get("action") or "")
        ev["delivery"] = delivery

        future = self.app.relay.submit_event(ev)
        try:
            results = future.result(timeout=self.app.cfg.publish_timeout_seconds)
        except concurrent.futures.TimeoutError:
            # cancel() 对已在运行的协程无效，但必须调：run_coroutine_threadsafe
            # 包装的协程还没被调度时它是能被取消的，省掉一次必然重复的门铃。
            future.cancel()
            self.app.seen.rollback(delivery)
            self._reply(504, "publish-timeout")
            return

        if any(not ok for _, ok in results):
            # 回滚：让平台用同一个 UUID 重试，而不是当成已处理丢弃。
            self.app.seen.rollback(delivery)
            self._reply(502, "publish-failed")
            return

        self.app.seen.commit(delivery)
        self._reply(200, "accepted")


def serve(app, listen):
    host, _, port = listen.rpartition(":")
    Handler.app = app
    httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler)
    # 没有超时的 socket 会被「声明 Content-Length 然后不发数据」的客户端
    # 无限期占住一个线程。10s 足够读完一个几 MB 的 webhook。
    httpd.timeout = 10
    httpd.daemon_threads = True
    return httpd