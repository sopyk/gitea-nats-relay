"""日志的护栏测试。

这里每一条都在钉一个**具体的失败模式**，不是为了覆盖率：

1. 目录不存在 / 不可写 → 服务照常起来（否则一个日志配置能搞挂生产）
2. 日志写失败 → webhook 照常 200，且 delivery 没有卡在 inflight
3. 隐私规则 → 正文和密钥不出现在任何日志里
4. 每个决策分支都留痕（改动 delete/rollback 的位置时会被抓到）
5. 限流 → 401 刷爆时日志不会无限长

第 2 条尤其重要：它同时钉住了「日志不打断主流程」和「日志不破坏去重
生命周期」两件事 —— 后者一旦坏掉，那个 delivery 会永久卡在 inflight，
平台每次重试都只拿到误导性的 409。
"""
import ast
import io
import json
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as config_mod  # noqa: E402
import logs as logs_mod  # noqa: E402
import server as server_mod  # noqa: E402


def capture(cfg):
    """按配置装配日志，把输出收进内存，返回 (stream, setup 的警告文本)。"""
    stream = io.StringIO()
    warning = logs_mod.setup(cfg, stream=stream)
    return stream, warning


class FormatterTest(unittest.TestCase):
    def test_text_format_is_greppable(self):
        stream, _ = capture(config_mod.LogConfig())
        logs_mod.event(logs_mod.get_logger(), logging.INFO, "请求处理完成",
                       provider="gitea", delivery="d-1", repo="acme/web",
                       number=134, status=200, decision="accepted")
        line = stream.getvalue()
        self.assertIn("provider=gitea", line)
        self.assertIn("delivery=d-1", line)
        self.assertIn("repo=acme/web", line)
        self.assertIn("number=134", line)
        self.assertIn("decision=accepted", line)
        # 时间戳必须自带时区偏移 —— 群晖上 TZ 是 Asia/Shanghai，
        # 只有裸时间的话跟其他服务的日志对不上。
        self.assertRegex(line, r"ts=\d{4}-\d{2}-\d{2}T[\d:.]+[+-]\d{2}:\d{2}")

    def test_values_with_spaces_are_quoted(self):
        """subject 里可能有空格，不加引号会让日志无法解析。"""
        stream, _ = capture(config_mod.LogConfig())
        logs_mod.event(logs_mod.get_logger(), logging.INFO, "x", note="a b c")
        self.assertIn('note="a b c"', stream.getvalue())

    def test_json_format_emits_one_object_per_line(self):
        stream, _ = capture(config_mod.LogConfig(format="json"))
        logs_mod.event(logs_mod.get_logger(), logging.WARNING, "验签失败",
                       delivery="d-2", status=401)
        lines = [l for l in stream.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, "JSON 格式必须每条一行")
        doc = json.loads(lines[0])
        self.assertEqual(doc["delivery"], "d-2")
        self.assertEqual(doc["level"], "WARNING")
        self.assertEqual(doc["status"], 401)


class PrivacyTest(unittest.TestCase):
    """隐私规则：日志里只允许出现已经发给 Agent 的字段。"""

    def test_forbidden_fields_are_stripped(self):
        stream, _ = capture(config_mod.LogConfig())
        logs_mod.event(
            logs_mod.get_logger(), logging.INFO, "x",
            repo="acme/web", number=1,          # 该留
            body="Secret A/B/C 泄露",            # 不该留
            secret="s3cr3t",                    # 不该留
            password="hunter2",                 # 不该留
            authorization="Bearer x",           # 不该留
        )
        out = stream.getvalue()
        self.assertIn("repo=acme/web", out)
        for leaked in ("Secret A/B/C", "s3cr3t", "hunter2", "Bearer x"):
            self.assertNotIn(leaked, out, f"敏感内容泄漏进了日志: {leaked}")

    def test_log_bodies_does_not_bypass_the_filter(self):
        """log_bodies 逃生开关也不能让 forbidden 字段混进来。

        它是另一条更显式的通路（server.py 里显式传 body），这里确保
        万一有人误传，正文依然被剥掉。
        """
        stream, _ = capture(config_mod.LogConfig(log_bodies=True))
        logs_mod.event(logs_mod.get_logger(), logging.DEBUG, "x", body="不该出现")
        self.assertNotIn("不该出现", stream.getvalue())


class DegradationTest(unittest.TestCase):
    """防护 1：日志文件不可用时必须降级，而不是让服务起不来。"""

    def test_unwritable_file_falls_back_to_stdout(self):
        d = tempfile.mkdtemp()
        blocked = os.path.join(d, "not-a-dir")
        with open(blocked, "w") as f:
            f.write("x")           # 用文件占住本该是目录的位置
        stream, warning = capture(
            config_mod.LogConfig(file=os.path.join(blocked, "relay.log")))
        self.assertNotEqual(warning, "", "降级了必须给出警告，不能静默")
        self.assertIn("stdout", warning)
        # 降级之后日志依然可用 —— 这才是重点
        logs_mod.event(logs_mod.get_logger(), logging.INFO, "仍然能记", a=1)
        self.assertIn("仍然能记", stream.getvalue())

    def test_parent_directory_is_created(self):
        """父目录不存在时自动建 —— RotatingFileHandler 构造时就会抛。"""
        d = tempfile.mkdtemp()
        path = os.path.join(d, "logs", "nested", "relay.log")
        stream, warning = capture(config_mod.LogConfig(file=path))
        self.assertEqual(warning, "", f"不该降级: {warning}")
        logs_mod.event(logs_mod.get_logger(), logging.INFO, "写进去了")
        self.assertTrue(os.path.exists(path), "日志文件没被创建")


class RateLimitTest(unittest.TestCase):
    def test_only_first_few_are_detailed(self):
        rl = logs_mod.RateLimiter(limit=3, window=60.0, aggregate_every=10)
        self.assertEqual([rl.allow() for _ in range(5)],
                         [True, True, True, False, False])

    def test_window_resets(self):
        rl = logs_mod.RateLimiter(limit=1, window=0.01)
        self.assertTrue(rl.allow())
        self.assertFalse(rl.allow())
        import time as _t
        _t.sleep(0.03)
        self.assertTrue(rl.allow(), "窗口过后应该恢复")

    def test_aggregate_is_taken_once(self):
        rl = logs_mod.RateLimiter(limit=1, window=60.0, aggregate_every=3)
        rl.allow()                    # 明细 1 条
        for _ in range(5):
            rl.allow()                # 被抑制 5 条
        # 攒够阈值就整批交出，且交出后归零 —— 否则聚合行会一直重复同一批数
        self.assertEqual(rl.take_aggregate(), 5)
        self.assertEqual(rl.take_aggregate(), 0, "聚合数不该被重复取走")

    def test_aggregate_below_threshold_is_zero(self):
        """还没攒够就不该吐聚合行，否则每条明细都跟一行聚合，白写两倍。"""
        rl = logs_mod.RateLimiter(limit=1, window=60.0, aggregate_every=10)
        rl.allow()
        rl.allow()
        self.assertEqual(rl.take_aggregate(), 0)


class RotatingTest(unittest.TestCase):
    def test_file_does_not_grow_without_bound(self):
        """「不要无限存」必须真的成立，而不只是文档里写了。"""
        d = tempfile.mkdtemp()
        path = os.path.join(d, "relay.log")
        capture(config_mod.LogConfig(file=path, max_bytes=2048, backups=2))
        for i in range(500):
            logs_mod.event(logs_mod.get_logger(), logging.INFO, "轮转测试",
                           idx=i, pad="x" * 100)
        total = sum(os.path.getsize(os.path.join(d, f))
                    for f in os.listdir(d))
        self.assertLess(total, 2048 * 4,
                        f"日志总量 {total} 字节，超过 max_bytes×(backups+1) 的上界")
        backups = [f for f in os.listdir(d) if ".relay.log." in f]
        self.assertLessEqual(len(backups), 2, "旧文件份数超过了 backups")


class LevelTest(unittest.TestCase):
    def test_level_filters(self):
        cfg = config_mod.LogConfig(level="warning")
        stream, _ = capture(cfg)
        lg = logs_mod.get_logger()
        logs_mod.event(lg, logging.INFO, "不该出现")
        logs_mod.event(lg, logging.WARNING, "该出现")
        out = stream.getvalue()
        self.assertNotIn("不该出现", out)
        self.assertIn("该出现", out)

    def test_setup_is_idempotent(self):
        """重复 setup 不能叠出一串 handler —— 否则每行日志会被打好几遍。"""
        d = tempfile.mkdtemp()
        path = os.path.join(d, "relay.log")
        for _ in range(3):
            capture(config_mod.LogConfig(file=path))
        stream, _ = capture(config_mod.LogConfig())
        logs_mod.event(logs_mod.get_logger(), logging.INFO, "只该出现一次")
        self.assertEqual(stream.getvalue().count("只该出现一次"), 1)
        self.assertEqual(len(logs_mod.get_logger().handlers), 1)


class ServerWiringTest(unittest.TestCase):
    """结构性护栏：确保日志没被插到去重生命周期中间。"""

    def test_no_log_call_between_begin_and_rollback(self):
        """begin() 与 rollback() 之间不允许出现日志调用。

        那段区间里 delivery 占着 inflight 占位。一旦日志抛异常导致跳过
        rollback，这个 delivery 会**永久卡住**，平台每次重试都只拿到
        误导性的 409（"上一次还在处理中"，实际上早就不处理了），
        直到进程重启。比没日志糟糕得多。
        """
        src = _read_server()
        handlers = _function_body(src, "_handle")
        self.assertIn("self.app.seen.begin(delivery)", handlers)

        # 在 _handle 里，begin 之后到每个 rollback 之前只允许有 reply/return。
        after_begin = handlers.split("self.app.seen.begin(delivery)", 1)[1]
        for call in ("logger.", "logs_mod.event(", "print("):
            self.assertNotIn(call, after_begin,
                             f"_handle 的 begin() 之后出现了日志调用 {call!r}")

    def test_reply_actually_logs(self):
        """_reply 里必须真的调用 _log_request，而不只是「函数里有 _reply」。

        这条是被 mutation 逼出来的：把 `self._log_request(...)` 整行删掉
        之后，源码扫描类断言全都照样通过 —— 它查的是「调用存在」，
        查不出「调用被摘掉了」。所以这里直接跑一次真实请求，断言
        响应确实留下了日志。
        """
        handler = _make_handler(app=_build_app()[0])
        rec = _call_reply(handler, 200, "accepted", delivery="d-9")
        self.assertTrue(rec, "_reply 没有落任何日志")
        text = _captured_text(rec)
        self.assertIn("decision=accepted", text)
        self.assertIn("delivery=d-9", text)
        self.assertIn("status=200", text)

    def test_bad_signature_is_rate_limited(self):
        """401 必须走限流器，且被抑制之后仍有聚合行。"""
        app, _ = _build_app()
        recs = []
        for i in range(12):
            recs += _call_reply(_make_handler(app=app), 401, "bad-signature",
                                delivery=f"d-{i}")
        detailed = [r for r in recs if "d-" in logs_mod.TextFormatter().format(r)]
        self.assertLessEqual(
            len(detailed), 6,
            f"401 记了 {len(detailed)} 条明细，限流失效了")
        self.assertTrue(recs, "401 一次都没记 —— 连信号都没留")

    def test_bad_signature_logs_at_warning_not_error(self):
        """401 是未认证请求，记 ERROR 等于给对方一个日志写放大器。"""
        recs = _call_reply(_make_handler(app=_build_app()[0]),
                           401, "bad-signature", delivery="d-x")
        self.assertTrue(recs)
        for r in recs:
            self.assertLessEqual(
                r.levelno, logging.WARNING,
                f"401 的日志级别是 {logging.getLevelName(r.levelno)}，"
                f"应为 WARNING 或更低 —— ERROR 会被未认证客户端刷爆")

    def test_reply_always_logs(self):
        """所有响应都必须经过 _reply —— 日志就落在那里。"""
        src = _read_server()
        for fn in ("_handle", "_process"):
            body = _function_body(src, fn)
            for guard in ("not-found", "bad-signature", "duplicate", "inflight",
                          "missing-delivery", "accepted", "publish-timeout",
                          "publish-failed", "ignored", "bad-json"):
                if guard in body:
                    self.assertIn("_reply", body,
                                  f"{fn} 里有 {guard} 分支却没有 _reply")

    def test_every_branch_reaches_reply(self):
        """每个 return 之前都该有一次 _reply，否则请求会既没日志也没响应。

        用 AST 而不是按缩进切文本。按缩进切会在重排版之后静默失效
        （测试还绿着，却已经不检查任何东西），而这里要防的恰恰是
        将来某次改动把某个分支的 _reply 挪掉。
        """
        tree = ast.parse(_read_server())
        checked = 0
        for fn in ("_handle", "_process", "do_POST", "do_GET"):
            node = _find_method(tree, fn)
            self.assertIsNotNone(node, f"server.py 里找不到 {fn}，扫描失效？")
            for ret in _returns(node):
                # return 之前可能隔着 _drain()（413 分支必须先回响应
                # 再关连接），所以往前多看两条，而不是只看紧邻的那句。
                window = _preceding_statements(node, ret, 2)
                self.assertTrue(
                    window, f"{fn} 里有一个 return 前面找不到语句")
                self.assertTrue(
                    any("_reply" in s for s in window),
                    f"{fn} 里有一个 return 之前没有 _reply，它前面是: "
                    f"{window}")
                checked += 1
        self.assertGreaterEqual(checked, 12,
                                f"只检查了 {checked} 个 return，扫描可能失效了")

    def test_branch_scan_actually_detects_a_missing_reply(self):
        """反向自检：把某个分支的 _reply 拿掉，这条断言必须失败。

        不这么做的话，「扫描只检查了 12 个 return」也可能是因为它压根
        没找到任何分支 —— 测试绿着，却什么也没保护。
        """
        broken = _server_source_with_reply_removed()
        tree = ast.parse(broken)
        node = _find_method(tree, "_process")
        offenders = [s for ret in _returns(node)
                     for s in _preceding_statements(node, ret, 2)
                     if "_reply" not in s]
        self.assertTrue(
            offenders,
            "把 accepted 分支的 _reply 删掉后，扫描仍然认为一切正常 —— "
            "说明这条测试是空转的")


def _server_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")


class _Cap(logging.Handler):
    """把日志记录攒进列表，供断言检查。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture_handler():
    """装配一个只往内存里记的 logger，返回 (handler, 恢复函数)。"""
    logger = logs_mod.get_logger()
    for h in list(logger.handlers):
        logger.removeHandler(h)
    cap = _Cap()
    logger.addHandler(cap)
    logger.setLevel(logging.DEBUG)

    def restore():
        logger.removeHandler(cap)
        logs_mod.setup(config_mod.LogConfig())

    return cap, restore


def _make_handler(app=None):
    """造一个只够跑 _reply 的 handler 实例。

    绕开真实 socket：_reply 只用到 wfile/send_response/headers，
    这些都可以用假对象顶掉 —— 我们要测的是「有没有落日志」，
    不是 HTTP 线上的字节。
    """
    handler = server_mod.Handler.__new__(server_mod.Handler)
    handler.app = app
    handler._started = None
    handler._fields = {}
    handler.client_address = ("192.0.2.9", 5555)
    handler.requestline = "POST /gitea-hook HTTP/1.1"
    handler.command = "POST"
    handler.path = "/gitea-hook"

    sent = []
    handler.wfile = _Writer(sent)
    handler.send_response = lambda code, *a, **k: sent.append(
        ("status", code))
    handler.send_header = lambda k, v: sent.append((k, v))
    handler.end_headers = lambda: None
    handler._sent = sent
    return handler


class _Writer:
    def __init__(self, sink):
        self.sink = sink

    def write(self, data):
        self.sink.append(("body", data))


def _call_reply(handler, code, msg, **kw):
    """跑一次 _reply，返回它产生的日志记录列表。"""
    cap, restore = _capture_handler()
    try:
        handler._reply(code, msg, **kw)
        return list(cap.records)
    finally:
        restore()


def _captured_text(records):
    """把记录按 text 格式渲染出来，便于做子串断言。"""
    fmt = logs_mod.TextFormatter()
    return "\n".join(fmt.format(r) for r in records)


def _build_app(publish=None):
    """一个够 _reply 用的 app 替身，带**自己的**限流器。

    必须给每个 app 独立限流器：共用模块全局会让测试之间互相偷配额，
    先跑的测试耗光额度后，后面的测试会误以为限流失效了。
    """
    class _Relay:
        def submit_event(self, ev):
            return publish or (lambda *_: [])

    app = server_mod.App.__new__(server_mod.App)
    app.cfg = config_mod.Config()
    app.relay = _Relay()
    app.bad_sig_limiter = logs_mod.RateLimiter(
        limit=5, window=60.0, aggregate_every=25)
    return app, None


def _read_server():
    with open(_server_path(), encoding="utf-8") as f:
        return f.read()


def _server_source_with_reply_removed():
    """把 _process 最后一行的 _reply 去掉，模拟「有人挪掉了日志落点」。"""
    src = _read_server()
    needle = 'self._reply(200, "accepted", delivery=delivery, subjects=subjects)'
    assert needle in src, (
        "反查的锚点没了 —— accepted 那行 _reply 的写法变了，"
        "请同步更新这个反向自检")
    return src.replace(needle, "pass  # 被测试故意删掉的 _reply", 1)


def _find_method(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _returns(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.Return)]


def _preceding_statements(func, ret, count):
    """return 之前的 count 条语句（跳过 docstring），从后往前取。"""
    for parent in ast.walk(func):
        for attr in ("body", "orelse", "finalbody"):
            body = getattr(parent, attr, None)
            if not isinstance(body, list) or ret not in body:
                continue
            out = []
            for prev in reversed(body[:body.index(ret)]):
                # docstring 在 AST 里是个字符串常量表达式，跳过
                if isinstance(prev, ast.Expr) and isinstance(
                        prev.value, ast.Constant):
                    continue
                out.append(ast.unparse(prev))
                if len(out) >= count:
                    return out
            return out
    return []



def _function_body(src, name):
    """取出一个函数的源码（含缩进）。给结构性断言用，够用即可。"""
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.strip().startswith(f"def {name}("))
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        out.append(line)
    return "\n".join(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)