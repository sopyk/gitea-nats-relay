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
    """假 NATS 客户端：记录发布到哪个 subject，可注入失败 / 超时。"""

    def __init__(self):
        class NC:
            is_connected = True
        self.nc = NC()
        self.published = []          # [(subject, event)]
        self.targets = []            # [(host, id)]，由用例设置
        self.fail_subjects = set()
        self.hang = False
        self.cancelled = 0

    def submit_event(self, ev):
        relay = self

        class Future:
            def result(inner, timeout=None):
                if relay.hang:
                    raise concurrent.futures.TimeoutError()
                out = []
                for host, aid in relay.targets:
                    subj = f"evt.gitea.notify.{host}.{aid}"
                    relay.published.append((subj, ev))
                    out.append((subj, subj not in relay.fail_subjects))
                return out

            def cancel(inner):
                relay.cancelled += 1
                return True

        return Future()


class HttpTestBase(unittest.TestCase):
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
        members_map = config_mod.load_members(cfg.members_file)
        self.relay = FakeRelay()

        app = server_mod.App(cfg, providers_mod.build_registry(cfg),
                             core.Members(members_map),
                             core.Seen(cfg.dedup_file, cfg.dedup_capacity), self.relay)
        self.httpd = server_mod.serve(app, "127.0.0.1:0")
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _http(self, req):
        """发一个原始 HTTP 请求并读回响应。

        服务端是 HTTP/1.1 keep-alive，连接不会自己关，所以必须按 Content-Length
        精确读 —— 读到 EOF 会一直等到超时。
        """
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
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
        p = fixtures.issue_comment(body="@mayaoyao 请处理，@unknown 忽略")
        self.relay.targets = [("h1", "mayaoyao")]
        status, body = self.post(p)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"accepted")
        subj, ev = self.relay.published[0]
        self.assertEqual(subj, "evt.gitea.notify.h1.mayaoyao")
        self.assertIn("mayaoyao", ev["mentions"])
        # 未注册的人不路由，但保留在 mentions 里供展示
        self.assertIn("unknown", ev["mentions"])

    def test_duplicate_delivery_does_not_republish(self):
        p = fixtures.issue_comment(body="@mayaoyao 看看")
        self.relay.targets = [("h1", "mayaoyao")]
        self.assertEqual(self.post(p, delivery="dup")[0], 200)
        self.assertEqual(self.post(p, delivery="dup")[1], b"duplicate")
        self.assertEqual(len(self.relay.published), 1)

    def test_missing_delivery_rejected(self):
        """没有 delivery 头就无法幂等 —— 放行等于每次重投都重复叫醒。"""
        p = fixtures.issue_comment(body="@mayaoyao 看看")
        self.relay.targets = [("h1", "mayaoyao")]
        status, body = self.post(p, no_delivery=True)
        self.assertEqual(status, 400)
        self.assertEqual(body, b"missing-delivery")
        self.assertEqual(len(self.relay.published), 0)

    def test_review_event_accepted(self):
        """旧实现会回 200 ignored，因为 EVENT_MAP 里没有正确的键。"""
        p = fixtures.review_approved()
        self.relay.targets = [("h1", "mayaoyao")]
        status, _ = self.post(p, event="pull_request_approved",
                              event_type="pull_request_review_approved")
        self.assertEqual(status, 200)
        _, ev = self.relay.published[0]
        self.assertEqual(ev["kind"], "review")
        self.assertEqual(ev["mentions"], ["mayaoyao"])
        self.assertEqual(ev["review_state"], "pull_request_review_approved")

    def test_review_request_routes_to_requested_reviewer(self):
        """请求评审：真正被请求的人在顶层单数字段里。"""
        p = fixtures.review_requested("madeng")
        self.relay.targets = [("h2", "madeng")]
        status, _ = self.post(p, event="pull_request",
                              event_type="pull_request_review_request")
        self.assertEqual(status, 200)
        _, ev = self.relay.published[0]
        self.assertEqual(ev["kind"], "review")
        self.assertIn("madeng", ev["reviewers"])

    def test_review_request_distinct_from_sync(self):
        """两者粗头都是 pull_request，靠细头区分。"""
        self.relay.targets = [("h2", "madeng")]
        p = fixtures.review_requested("madeng")
        s1, _ = self.post(p, event="pull_request",
                          event_type="pull_request_review_request", delivery="rr")
        self.assertEqual(s1, 200)
        p2 = fixtures.pull_request_event(action="synchronized")
        s2, _ = self.post(p2, event="pull_request",
                          event_type="pull_request_sync", delivery="sy")
        self.assertEqual(s2, 200)
        self.assertEqual(self.relay.published[0][1]["kind"], "review")
        self.assertEqual(self.relay.published[1][1]["kind"], "pull_request")

    def test_unknown_event_ignored_not_dropped(self):
        status, body = self.post(fixtures.issue_comment(), event="push", delivery="x1")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ignored")
        self.assertEqual(len(self.relay.published), 0)
        # 已标记处理，平台不会无意义重试
        self.assertEqual(self.post(fixtures.issue_comment(), event="push",
                                   delivery="x1")[1], b"duplicate")

    def test_bad_signature_rejected(self):
        status, body = self.post(fixtures.issue_comment(), secret="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(body, b"bad-signature")
        self.assertEqual(len(self.relay.published), 0)

    def test_no_signature_header_rejected(self):
        status, _ = self.post(fixtures.issue_comment(), no_signature=True)
        self.assertEqual(status, 401)

    def test_tampered_body_rejected(self):
        """签名之后改动请求体 —— 必须被拒。"""
        p = fixtures.issue_comment(body="@mayaoyao 看")
        body = json.dumps(p, ensure_ascii=False).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        tampered = body.replace(b"\xe7\x9c\x8b", b"\xe4\xbf\xae")  # 看 → 修
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
        self.assertEqual(body, b"bad-json")
        # 回滚了 delivery，同一 UUID 可以重来
        self.assertEqual(self.post(None, raw_body=b"{not json")[0], 400)

    def test_json_array_rejected(self):
        status, _ = self.post(None, raw_body=b"[1,2,3]")
        self.assertEqual(status, 400)

    def test_publish_failure_rolls_back_and_allows_retry(self):
        p = fixtures.issue_comment(body="@mayaoyao 看看")
        self.relay.targets = [("h1", "mayaoyao")]
        self.relay.fail_subjects = {"evt.gitea.notify.h1.mayaoyao"}
        self.assertEqual(self.post(p, delivery="f1"), (502, b"publish-failed"))
        # 同一 delivery 必须还能重试，不能被当重复丢弃
        self.relay.fail_subjects = set()
        self.assertEqual(self.post(p, delivery="f1"), (200, b"accepted"))
        self.assertEqual(self.post(p, delivery="f1"), (200, b"duplicate"))

    def test_publish_timeout_rolls_back(self):
        p = fixtures.issue_comment(body="@mayaoyao 看看")
        self.relay.targets = [("h1", "mayaoyao")]
        self.relay.hang = True
        status, body = self.post(p, delivery="t1")
        self.assertEqual(status, 504)
        self.assertEqual(body, b"publish-timeout")
        self.assertGreaterEqual(self.relay.cancelled, 1,
                                "超时应尝试 cancel，避免协程继续跑完")
        self.relay.hang = False
        self.assertEqual(self.post(p, delivery="t1"), (200, b"accepted"))

    def test_bad_content_length_does_not_kill_connection_silently(self):
        """旧实现 int() 抛 ValueError 逃出 handler → 连接被重置，客户端零响应。"""
        status, body = self.post(fixtures.issue_comment(), bad_length=True)
        self.assertEqual(status, 400)
        self.assertEqual(body, b"bad-length")

    def test_oversized_body_rejected_before_read(self):
        status, body = self.post(None, raw_body=b"x" * (2 * 1024 * 1024))
        self.assertEqual(status, 413)
        self.assertEqual(body, b"payload-too-large")

    def test_unknown_path_404(self):
        self.assertEqual(self.post(fixtures.issue_comment(), path="/nope")[0], 404)

    def test_dedup_survives_restart(self):
        p = fixtures.issue_comment(body="@mayaoyao 看看")
        self.relay.targets = [("h1", "mayaoyao")]
        self.assertEqual(self.post(p, delivery="persist")[0], 200)
        app = self.httpd.RequestHandlerClass.app
        fresh = core.Seen(app.seen.path, app.seen.cap)
        self.assertEqual(fresh.begin("persist"), "seen")

    # ── 回归用例：下面每一条都对应一个真实踩过的坑 ──────────────

    def test_malformed_payload_does_not_leak_inflight(self):
        """归一化抛异常时占位必须被释放。

        否则同一个 delivery 会被永久卡在 inflight，平台每次重试都只拿到
        一个误导性的 409 inflight，直到进程重启 —— 通知永久丢失。
        """
        app = self.httpd.RequestHandlerClass.app
        broken = {"repository": "not-a-dict", "sender": {"login": "you"},
                  "issue": {"number": 1}}
        status, _ = self.post(broken, event="issues", delivery="LEAK")
        # _obj 守卫挡住了崩溃，所以这里是正常的 200 —— 但无论如何都不能留下 inflight
        self.assertIn(status, (200, 500))
        self.assertNotIn("LEAK", app.seen.inflight, "inflight 占位泄漏了")
        # 重投要么被正常处理（reserved）、要么已被处理（seen），
        # 唯一不可接受的是永远 inflight。
        self.assertIn(app.seen.begin("LEAK"), ("reserved", "seen"))

    def test_unexpected_error_returns_500_not_reset(self):
        """未预期异常必须变成诚实的 500，而不是连接被重置（客户端零响应）。"""
        app = self.httpd.RequestHandlerClass.app
        boom = lambda ev: (_ for _ in ()).throw(RuntimeError("boom"))
        app.registry["/gitea-hook"].normalizer = boom
        try:
            status, body = self.post(fixtures.issue_comment(), delivery="BOOM")
            self.assertEqual(status, 500)
            self.assertEqual(body, b"publish-error")
            self.assertNotIn("BOOM", app.seen.inflight)
        finally:
            app.registry["/gitea-hook"].normalizer = providers_mod.normalize_gitea

    def test_non_dict_fields_do_not_crash_normalization(self):
        """平台改了 payload 结构不能变成永久丢事件。"""
        app = self.httpd.RequestHandlerClass.app
        self.relay.targets = [("h1", "mayaoyao")]
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
                self.assertEqual(app.seen.begin(f"m{i}"), "seen" if st == 200 else "reserved")

    def test_non_ascii_signature_rejected_cleanly(self):
        """HTTP 头按 latin-1 解码，>=0x80 的字节让 compare_digest 抛 TypeError。

        旧实现里这会让异常逃出去：客户端连接被重置（零响应），
        而且每个这样的请求都会往 stderr 打一条 traceback。
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
        self.assertEqual(resp, b"bad-signature")

    def test_verify_rejects_non_ascii_directly(self):
        import providers as P
        cfg = config_mod.load(path="/nonexistent", env={
            "NATS_SERVERS": "nats://h:4222", "NATS_USER": "u",
            "NATS_PASSWORD": "p", "WEBHOOK_SECRET": "sec"})
        prov = P.build_registry(cfg)["/gitea-hook"]
        self.assertFalse(prov.verify(b"{}", {"X-Gitea-Signature": "ä"}))

    def test_bad_json_never_sticks_in_inflight(self):
        app = self.httpd.RequestHandlerClass.app
        self.assertEqual(self.post(None, raw_body=b"{bad")[0], 400)
        self.assertEqual(app.seen.begin("bj"), "reserved")


class HealthTest(HttpTestBase):
    def test_healthz_ok(self):
        status, body = self._http(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")

    def test_unknown_get_404(self):
        status, _ = self._http(b"GET /nope HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)