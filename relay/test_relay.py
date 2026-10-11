#!/usr/bin/env python3
"""HTTP 层端到端行为测试。

起真的 ThreadingHTTPServer，走真的 socket，用真的 HMAC 签名 —— 只有 NATS
是假的。旧测试是直接构造 Handler 实例再 `__new__` 塞属性，绕过了 socket、
Content-Length 解析、协议版本这些东西，测不到真实路径。

运行：python3 test_relay.py
"""
import concurrent.futures
import hashlib
import hmac
import json
import re
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.modules.setdefault("nats", types.ModuleType("nats"))

import config as config_mod
import core
import fixtures
import providers as providers_mod
import server as server_mod

SECRET = "s3cr3t"

TOML = """
[service]
listen = "127.0.0.1:0"

[nats]
servers = ["nats://fake:4222"]
user = "u"
password = "p"

[routing]
members_file = "{members}"
broadcast_authors = ["mayaoyao"]

[dedup]
file = "{dedup}"

[providers.gitea]
path = "/gitea-hook"
[providers.gitea.headers]
event = "X-Gitea-Event"
event_type = "X-Gitea-Event-Type"
delivery = "X-Gitea-Delivery"
signature = "X-Gitea-Signature"
[providers.gitea.signature]
algorithm = "hmac-sha256"
secret = "{secret}"
[providers.gitea.events]
issues = "issue"
issue_comment = "issue_comment"
pull_request = "pull_request"
pull_request_approved = "review"
pull_request_rejected = "review"
pull_request_comment = "review"
[providers.gitea.event_types]
pull_request_review_request = "review"
"""


class FakeRelay:
    """用真实的 subjects_for / extract_targets 算收件位。

    之前的版本拿「测试自己塞进去的 targets 列表」拼 subject，绕过了
    extract_targets —— 于是把 extract_targets 改成永远返回 [] 时，
    26 条端到端测试全绿。断言的是 mock 回吐了测试自己的输入，等于没测。
    现在这条路径必须真的走产品代码。
    """

    def __init__(self, cfg, members):
        import app
        self._subjects = app.Relay.subjects_for
        self.cfg = cfg
        self.members = members
        self.published = []          # [(subject, event)]
        self.fail_subjects = set()
        self.hang = False
        self.cancelled = 0

        class NC:
            is_connected = True
        self.nc = NC()

    def subjects_for(self, ev):
        # 借用真实的 Relay.subjects_for，但绑定到 fake 的失败/超时开关上
        return self._subjects(_Shim(self), ev)

    def submit_event(self, ev):
        relay = self

        class Future:
            def result(inner, timeout=None):
                if relay.hang:
                    raise concurrent.futures.TimeoutError()
                out = []
                for subj in relay.subjects_for(ev):
                    relay.published.append((subj, ev))
                    out.append((subj, subj not in relay.fail_subjects))
                return out

            def cancel(inner):
                relay.cancelled += 1
                return True

        return Future()


class _Shim:
    """让真实的 Relay.subjects_for 能跑在 FakeRelay 上。

    只借方法，不构造真连接 —— subjects_for 是纯函数，不碰 NATS。
    """
    def __init__(self, fake):
        self.cfg = fake.cfg
        self.members = fake.members


class HttpTestBase(unittest.TestCase):
    @staticmethod
    def err_of(body):
        """从响应体取状态名。

        响应契约是 JSON（见 README「HTTP 契约」）：成功给 `status`，
        失败给 `error`。测试断言这个字段而不是整段字节 —— 整段字节断言
        会在响应里多带一个 delivery 时集体变红，却看不出是什么变了。
        """
        doc = json.loads(body)
        return doc.get("error") or doc.get("status")
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        members = os.path.join(self.tmp, "members.json")
        with open(members, "w", encoding="utf-8") as f:
            json.dump({"members": {
                "mayaoyao": {"host": "h1", "id": "mayaoyao"},
                "madeng": {"host": "h2", "id": "madeng"},
            }}, f)
        cfg_path = os.path.join(self.tmp, "c.toml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(TOML.format(members=members, secret=SECRET,
                                dedup=os.path.join(self.tmp, "data", "seen.json")))

        cfg = config_mod.load(path=cfg_path, env={})
        self.members = core.Members(config_mod.load_members(cfg.members_file))
        self.relay = FakeRelay(cfg, self.members)

        app = server_mod.App(cfg, providers_mod.build_registry(cfg),
                             self.members,
                             core.Seen(cfg.dedup_file, cfg.dedup_capacity), self.relay)
        self.httpd = server_mod.serve(app, "127.0.0.1:0")
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _pipelined(self, reqs):
        """把多个请求放在**同一条 TCP 连接**上依次发出。

        这是 keep-alive 污染问题的唯一有效测法：每次 _http() 都是新连接，
        所以「残留字节被当成下一个请求行」这种 bug 在那里根本测不出来。
        返回每个响应解析出的 (status, body)。
        """
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        out = []
        try:
            s.settimeout(5)
            for req in reqs:
                head, sep, body = req.partition(b"\r\n\r\n")
                try:
                    s.sendall(head + sep)
                    if len(body) <= 64 * 1024:
                        s.sendall(body)
                    else:
                        try:
                            s.sendall(body)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                except (BrokenPipeError, ConnectionResetError):
                    pass  # 服务端决定关闭这条连接，后续请求改用新连接
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    out.append((None, b""))
                    break
                head_b, _, rest = buf.partition(b"\r\n\r\n")
                m = re.search(rb"Content-Length:\s*(\d+)", head_b, re.I)
                n = int(m.group(1)) if m else 0
                while len(rest) < n:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
                out.append((int(head_b.split(b" ")[1]), rest[:n] if n else b""))
                if b"Connection: close" in head_b:
                    break
            return out
        finally:
            s.close()

    def _raw(self, body, path="/gitea-hook", event="issues", delivery="d"):
        """构造一个签名单字面量的原始请求（不二次改写 body）。"""
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return (f"POST {path} HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"X-Gitea-Delivery: {delivery}\r\n"
                f"X-Gitea-Signature: {sig}\r\n"
                f"X-Gitea-Event: {event}\r\n\r\n").encode() + body

    def _signed_post(self, payload, delivery, event="issue_comment"):
        body = json.dumps(payload, ensure_ascii=False).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return (f"POST /gitea-hook HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"X-Gitea-Delivery: {delivery}\r\n"
                f"X-Gitea-Signature: {sig}\r\n"
                f"X-Gitea-Event: {event}\r\n\r\n").encode() + body

    def _http(self, req):
        """发一个原始 HTTP 请求并读回响应。

        服务端是 HTTP/1.1 keep-alive，连接不会自己关，所以必须按 Content-Length
        精确读 —— 读到 EOF 会一直等到超时。

        大 body 必须**先发 header 再发 body**：服务端看到 Content-Length 超限会
        立刻回 413 并关连接，而客户端此时还在 sendall 剩下的 2MB，
        sendall 就会抛 BrokenPipeError。这是客户端写法问题，与被测服务无关。
        """
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            head, sep, body = req.partition(b"\r\n\r\n")
            if len(body) > 64 * 1024:
                # 大 body 分开发：先发 header，服务端看到 Content-Length 超限
                # 会立刻回 413 并准备关闭；客户端这会儿还在发剩下的字节，
                # sendall 撞上关闭就会 BrokenPipeError。
                # 那是**协议预期内**的行为（服务端拒绝收这个 body），
                # 不是被测代码的缺陷 —— 真实客户端也一样会撞上。
                s.sendall(head + sep)
                try:
                    s.sendall(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # 服务端已回 413 并关闭，继续往下读响应即可
            else:
                s.sendall(req)
            s.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    return None, b""
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
            n = int(m.group(1)) if m else 0
            while len(rest) < n:
                chunk = s.recv(65536)
                if not chunk:
                    break
                rest += chunk
            status = int(head.split(b" ")[1])
            return status, (rest[:n] if n else b"")
        finally:
            s.close()

    def post(self, payload, event="issue_comment", event_type=None,
             delivery="d1", secret=SECRET, raw_body=None, path="/gitea-hook",
             bad_length=False, no_delivery=False, no_signature=False):
        body = raw_body if raw_body is not None else json.dumps(
            payload, ensure_ascii=False).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        head = [f"POST {path} HTTP/1.1", "Host: localhost", "Content-Type: application/json"]
        if bad_length:
            head.append("Content-Length: not-a-number")
        else:
            head.append(f"Content-Length: {len(body)}")
        if not no_delivery:
            head.append(f"X-Gitea-Delivery: {delivery}")
        if not no_signature:
            head.append(f"X-Gitea-Signature: {sig}")
        if event:
            head.append(f"X-Gitea-Event: {event}")
        if event_type:
            head.append(f"X-Gitea-Event-Type: {event_type}")
        return self._http(("\r\n".join(head) + "\r\n\r\n").encode() + body)


class WebhookBehaviorTest(HttpTestBase):
    def test_comment_routes_to_mention(self):
        """核心路径：评论里的 @提及 → 定向 subject。

        路由走真实的 Relay.subjects_for + core.extract_targets ——
        之前的 fake 是拿测试自设的 targets 拼 subject，把 extract_targets
        改成永远返回 [] 时全套测试依然全绿。
        """
        p = fixtures.issue_comment(body="@mayaoyao 请处理，@unknown 忽略")
        status, body = self.post(p)
        self.assertEqual(status, 200)
        self.assertEqual(self.err_of(body), "accepted")
        subjects = [s for s, _ in self.relay.published]
        self.assertEqual(subjects, ["evt.gitea.notify.h1.mayaoyao"])
        _, ev = self.relay.published[0]
        self.assertIn("mayaoyao", ev["mentions"])
        # 未注册的人不路由，但保留在 mentions 里供展示
        self.assertIn("unknown", ev["mentions"])

    def test_mention_and_assignee_both_notified(self):
        p = fixtures.issue_comment(body="@madeng 你也看看")
        p["issue"]["assignees"] = [{"login": "mayaoyao"}]
        self.post(p)
        subjects = sorted(s for s, _ in self.relay.published)
        self.assertEqual(subjects, ["evt.gitea.notify.h1.mayaoyao",
                                    "evt.gitea.notify.h2.madeng"])

    def test_unmapped_login_publishes_nothing(self):
        """不在成员映射里的人**不该**收到任何门铃。

        payload 里的 assignees 默认是 mayaoyao，这里要清掉，否则它仍会被通知，
        断言就测不出「未知 @提及不路由」了。
        """
        p = fixtures.issue_comment(body="@nobody 在吗")
        p["issue"]["assignees"] = []
        self.assertEqual(self.post(p)[0], 200)
        self.assertEqual(self.relay.published, [],
                         "未注册的 @提及不该产生任何发布")

    def test_duplicate_delivery_does_not_republish(self):
        p = fixtures.issue_comment(body="@mayaoyao 看看")
        self.assertEqual(self.post(p, delivery="dup")[0], 200)
        self.assertEqual(self.err_of(self.post(p, delivery="dup")[1]), "duplicate")
        self.assertEqual(len(self.relay.published), 1)

    def test_missing_delivery_rejected(self):
        """没有 delivery 头就无法幂等 —— 放行等于每次重投都重复叫醒。"""
        p = fixtures.issue_comment(body="@mayaoyao 看看")
        status, body = self.post(p, no_delivery=True)
        self.assertEqual(status, 400)
        self.assertEqual(self.err_of(body), "missing-delivery")
        self.assertEqual(len(self.relay.published), 0)

    def test_review_event_accepted(self):
        """旧实现会回 200 ignored，因为 EVENT_MAP 里没有正确的键。"""
        p = fixtures.review_approved()
        status, _ = self.post(p, event="pull_request_approved",
                              event_type="pull_request_review_approved")
        self.assertEqual(status, 200)
        _, ev = self.relay.published[0]
        self.assertEqual(ev["kind"], "review")
        self.assertEqual(ev["mentions"], ["mayaoyao"])
        self.assertEqual(ev["review_state"], "pull_request_review_approved")
        # 评审正文里的 @提及要能路由到人（读的是 review.content 不是 body）。
        # 收件人有两个来源：正文里的 @mayaoyao，以及 PR 对象上的评审人 madeng。
        subjects = sorted(s for s, _ in self.relay.published)
        self.assertEqual(subjects, ["evt.gitea.notify.h1.mayaoyao",
                                    "evt.gitea.notify.h2.madeng"])

    def test_review_request_routes_to_requested_reviewer(self):
        """请求评审：真正被请求的人在顶层 requested_reviewer 单数字段里，
        此时 PR 上的 requested_reviewers 是空的 —— 读不到它就通知不到人。"""
        p = fixtures.review_requested("madeng")
        self.assertEqual(p["pull_request"]["requested_reviewers"], [],
                         "fixture 前提：PR 自身的评审人列表此时应为空")
        status, _ = self.post(p, event="pull_request",
                              event_type="pull_request_review_request")
        self.assertEqual(status, 200)
        _, ev = self.relay.published[0]
        self.assertEqual(ev["kind"], "review")
        self.assertIn("madeng", ev["reviewers"],
                      "被指派的评审人必须出现在收件人里")
        subjects = sorted(s for s, _ in self.relay.published)
        self.assertIn("evt.gitea.notify.h2.madeng", subjects,
                      "请求评审必须通知到被指派的那个人")

    def test_bad_signature_rejected(self):
        status, body = self.post(fixtures.issue_comment(), secret="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(self.err_of(body), "bad-signature")
        self.assertEqual(len(self.relay.published), 0)

    def test_no_signature_header_rejected(self):
        status, _ = self.post(fixtures.issue_comment(), no_signature=True)
        self.assertEqual(status, 401)
        self.assertEqual(len(self.relay.published), 0)

    def test_tampered_body_rejected(self):
        """签名之后改动请求体 —— 必须被拒。"""
        p = fixtures.issue_comment(body="@mayaoyao 看")
        body = json.dumps(p, ensure_ascii=False).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        tampered = body.replace("看".encode(), "修".encode())
        head = (f"POST /gitea-hook HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {len(tampered)}\r\n"
                f"X-Gitea-Delivery: tam\r\n"
                f"X-Gitea-Signature: {sig}\r\n"
                f"X-Gitea-Event: issue_comment\r\n\r\n").encode()
        status, _ = self._http(head + tampered)
        self.assertEqual(status, 401)
        self.assertNotEqual(tampered, body)

    def test_bad_json_returns_400_and_allows_retry(self):
        status, body = self.post(None, raw_body=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(self.err_of(body), "bad-json")
        # 回滚了 delivery，同一 UUID 可以重来
        self.assertEqual(self.post(None, raw_body=b"{not json")[0], 400)

    def test_json_array_rejected(self):
        status, _ = self.post(None, raw_body=b"[1,2,3]")
        self.assertEqual(status, 400)

    def test_publish_failure_rolls_back_and_allows_retry(self):
        p = fixtures.issue_comment(body="@mayaoyao 看看")
        self.relay.fail_subjects = {"evt.gitea.notify.h1.mayaoyao"}
        status, body = self.post(p, delivery="f1")
        self.assertEqual((status, self.err_of(body)), (502, "publish-failed"))
        # 同一 delivery 必须还能重试，不能被当重复丢弃
        self.relay.fail_subjects = set()
        status, body = self.post(p, delivery="f1")
        self.assertEqual((status, self.err_of(body)), (200, "accepted"))
        self.assertEqual(self.err_of(self.post(p, delivery="f1")[1]), "duplicate")

    def test_publish_timeout_rolls_back_and_allows_retry(self):
        """超时后 delivery 必须回滚，平台能用同一 UUID 重投。"""
        p = fixtures.issue_comment(body="@mayaoyao 看看")
        self.relay.hang = True
        status, body = self.post(p, delivery="t1")
        self.assertEqual(status, 504)
        self.assertEqual(self.err_of(body), "publish-timeout")
        self.assertGreaterEqual(self.relay.cancelled, 1, "超时应尝试 cancel")
        app = self.httpd.RequestHandlerClass.app
        self.assertNotIn("t1", app.seen.inflight)
        self.relay.hang = False
        status, body = self.post(p, delivery="t1")
        self.assertEqual((status, self.err_of(body)), (200, "accepted"))

    def test_unknown_event_ignored_not_dropped(self):
        status, body = self.post(fixtures.issue_comment(), event="push", delivery="x1")
        self.assertEqual(status, 200)
        self.assertEqual(self.err_of(body), "ignored")
        self.assertEqual(self.relay.published, [])
        self.assertEqual(self.err_of(self.post(fixtures.issue_comment(), event="push",
                                                   delivery="x1")[1]), "duplicate")

    def test_unknown_path_404(self):
        self.assertEqual(self.post(fixtures.issue_comment(), path="/nope")[0], 404)

    def test_dedup_survives_restart(self):
        p = fixtures.issue_comment(body="@mayaoyao 看看")
        self.assertEqual(self.post(p, delivery="persist")[0], 200)
        app = self.httpd.RequestHandlerClass.app
        fresh = core.Seen(app.seen.path, app.seen.cap)
        self.assertEqual(fresh.begin("persist"), "seen")

    def test_bad_json_never_sticks_in_inflight(self):
        app = self.httpd.RequestHandlerClass.app  # serve() 绑定的子类
        self.assertEqual(self.post(None, raw_body=b"{bad")[0], 400)
        self.assertEqual(app.seen.begin("bj"), "reserved")

    def test_malformed_payload_does_not_leak_inflight(self):
        """归一化抛异常时占位必须被释放。

        否则同一个 delivery 会被永久卡在 inflight，平台每次重试都只拿到
        一个误导性的 409 inflight，直到进程重启 —— 通知永久丢失。
        """
        app = self.httpd.RequestHandlerClass.app
        broken = {"repository": "not-a-dict", "sender": {"login": "you"},
                  "issue": {"number": 1}}
        status, _ = self.post(broken, event="issues", delivery="LEAK")
        self.assertIn(status, (200, 500))
        self.assertNotIn("LEAK", app.seen.inflight, "inflight 占位泄漏了")
        self.assertIn(app.seen.begin("LEAK"), ("reserved", "seen"))

    def test_unexpected_error_returns_500_not_reset(self):
        """未预期异常必须变成诚实的 500，而不是连接被重置（客户端零响应）。"""
        app = self.httpd.RequestHandlerClass.app
        original = app.registry["/gitea-hook"].normalizer
        app.registry["/gitea-hook"].normalizer = lambda ev: (_ for _ in ()).throw(
            RuntimeError("boom"))
        try:
            status, body = self.post(fixtures.issue_comment(), delivery="BOOM")
            self.assertEqual(status, 500)
            self.assertEqual(self.err_of(body), "publish-error")
            self.assertNotIn("BOOM", app.seen.inflight)
        finally:
            app.registry["/gitea-hook"].normalizer = original

    def test_non_dict_fields_do_not_crash_normalization(self):
        app = self.httpd.RequestHandlerClass.app
        for i, payload in enumerate([
            {"repository": "str", "sender": "str", "issue": "str"},
            {"repository": ["list"], "sender": 42, "issue": {"assignees": "not-a-list"}},
            {"comment": "str", "issue": {"labels": "not-a-list"}},
            {"pull_request": 7, "requested_reviewer": "str"},
            {"review": "str", "issue": {"labels": [None, "x", {"name": "ok"}]}},
        ]):
            with self.subTest(i=i):
                st, _ = self.post(payload, event="issue_comment", delivery=f"m{i}")
                self.assertIn(st, (200, 500))
                self.assertEqual(app.seen.begin(f"m{i}"),
                                 "seen" if st == 200 else "reserved")

    def test_non_ascii_signature_rejected_cleanly(self):
        """HTTP 头按 latin-1 解码，>=0x80 的字节让 compare_digest 抛 TypeError。

        直接发含 0xE4 的原始字节，绕过 BaseHTTPRequestHandler 的规范化。
        """
        body = b'{"issue":{"number":1}}'
        head = (f"POST /gitea-hook HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"X-Gitea-Delivery: na\r\n"
                f"X-Gitea-Event: issues\r\n"
                f"X-Gitea-Signature: \xe4\xe4\r\n\r\n").encode("latin-1")
        status, resp = self._http(head + body)
        self.assertEqual(status, 401, "非 ASCII 签名头应被干净地拒绝为 401")
        self.assertEqual(self.err_of(resp), "bad-signature")

    def test_socket_timeout_is_configured(self):
        """per-connection 超时必须真的设上。

        BaseHTTPRequestHandler.timeout 默认是 None（永不超时），
        一个「声明 Content-Length 然后不发数据」的未认证客户端就能
        永久占住一个线程 —— ThreadingHTTPServer 不限线程数。
        """
        cls = self.httpd.RequestHandlerClass
        self.assertIsNotNone(cls.timeout,
                             "Handler.timeout 没设，慢连接可无限占线程")
        self.assertLessEqual(cls.timeout, 30)

    def test_verify_rejects_non_ascii_directly(self):
        cfg = config_mod.load(path="/nonexistent", env={
            "NATS_SERVERS": "nats://h:4222", "NATS_USER": "u",
            "NATS_PASSWORD": "p", "WEBHOOK_SECRET": "sec"})
        prov = providers_mod.build_registry(cfg)["/gitea-hook"]
        self.assertFalse(prov.verify(b"{}", {"X-Gitea-Signature": "ä"}))

    def test_bad_length_closes_connection(self):
        """Content-Length 非法时必须关连接 —— 客户端声称要发多少字节不可知，
        无法确定还剩多少没读，复用连接必然错乱。

        所以这条测的是「诚实地关掉」，不是「保持 keep-alive」。
        """
        bad = (b"POST /gitea-hook HTTP/1.1\r\nHost: x\r\n"
               b"Content-Length: not-a-number\r\nX-Gitea-Event: issues\r\n\r\n")
        results = self._pipelined([bad])
        self.assertEqual(results[0][0], 400)
        self.assertEqual(self.err_of(results[0][1]), "bad-length")

    def test_drain_protects_keep_alive_after_404(self):
        """404 路径必须 drain，否则下一个请求被残余字节污染。

        变异测试：把 _drain() 改成 no-op 时这条会红。
        """
        body = b'{"repository":1}'
        bad = (f"POST /nope HTTP/1.1\r\nHost: x\r\n"
               f"X-Gitea-Delivery: nf\r\n"
               f"X-Gitea-Event: issues\r\n"
               f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        results = self._pipelined([bad, self._signed_post(
            fixtures.issue_comment(body="@mayaoyao 看看"), "after-404")])
        self.assertEqual(results[0][0], 404)
        self.assertEqual(len(results), 2, "404 之后连接应继续可用")
        self.assertEqual(results[1][0], 200,
                         f"keep-alive 被污染: {results[1][1]!r}")

    def test_broadcast_announcement_reaches_broadcast_subject(self):
        """公告必须真的发到 broadcast subject。

        变异测试：把 is_broadcast 改成永远 False 时这条必须红。
        """
        ev = fixtures.issue_event(action="opened", actor="mayaoyao")
        ev["issue"] = fixtures._issue(title="【公告】今晚维护")
        ev["issue"]["assignees"] = []
        status, _ = self.post(ev, event="issues", delivery="bc1")
        self.assertEqual(status, 200)
        subjects = [s for s, _ in self.relay.published]
        self.assertIn("evt.gitea.broadcast", subjects,
                      "配置作者新建带【公告】标题的 issue 应走全厅")

    def test_broadcast_by_label(self):
        """带「公告」标签即可，不必同时有标题前缀。"""
        ev = fixtures.issue_event(action="opened", actor="mayaoyao")
        ev["issue"] = fixtures._issue(title="普通标题")
        ev["issue"]["assignees"] = []
        ev["issue"]["labels"] = [{"id": 9, "name": "公告"}]
        self.post(ev, event="issues", delivery="bc2")
        subjects = [s for s, _ in self.relay.published]
        self.assertIn("evt.gitea.broadcast", subjects)

    def test_non_author_announcement_is_not_broadcast(self):
        """非配置作者的 issue 即使带公告标题也不该全厅打扰。"""
        ev = fixtures.issue_event(action="opened", actor="madeng")
        ev["issue"] = fixtures._issue(title="【公告】标题")
        ev["issue"]["assignees"] = []
        self.post(ev, event="issues", delivery="bc3")
        subjects = [s for s, _ in self.relay.published]
        self.assertNotIn("evt.gitea.broadcast", subjects)

    def test_comment_is_not_broadcast(self):
        """只有 issue 新建/重开才广播；评论、请求评审都不该。"""
        self.post(fixtures.issue_comment(body="@mayaoyao 看看"), delivery="bc4")
        subjects = [s for s, _ in self.relay.published]
        self.assertNotIn("evt.gitea.broadcast", subjects)

    def test_reopened_announcement_is_broadcast(self):
        ev = fixtures.issue_event(action="reopened", actor="mayaoyao")
        ev["issue"] = fixtures._issue(title="【公告】重新上线")
        ev["issue"]["assignees"] = []
        self.post(ev, event="issues", delivery="bc5")
        subjects = [s for s, _ in self.relay.published]
        self.assertIn("evt.gitea.broadcast", subjects)

    def test_keep_alive_handles_multiple_valid_requests(self):
        """基线：keep-alive 本身能连续处理多个请求。"""
        results = self._pipelined([
            self._signed_post(fixtures.issue_comment(body="@mayaoyao 一"), "ka1"),
            self._signed_post(fixtures.issue_comment(body="@mayaoyao 二"), "ka2"),
            self._signed_post(fixtures.issue_comment(body="@mayaoyao 三"), "ka3"),
        ])
        self.assertEqual([r[0] for r in results], [200, 200, 200])

    def test_413_closes_connection_cleanly(self):
        """超大 body 必须回 413（而不是连接重置）并关闭连接。

        顺序反了的话客户端只看到 RST，拿不到状态码，也无从知道是超限。
        """
        big = b"x" * (server_mod.MAX_BODY_BYTES + 4096)
        status, body = self._http(self._raw(big, delivery="big", event="issues"))
        self.assertEqual(status, 413)
        self.assertEqual(self.err_of(body), "payload-too-large")


class HealthTest(HttpTestBase):
    def test_healthz_ok(self):
        status, body = self._http(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertEqual(self.err_of(body), "ok")

    def test_unknown_get_404(self):
        status, _ = self._http(b"GET /nope HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)