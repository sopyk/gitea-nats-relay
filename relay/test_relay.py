#!/usr/bin/env python3
"""gitea-nats-relay 行为测试。

不依赖真实 NATS / HTTP 网络：用内存 FakeNats 与直接调用 handler 模拟 Gitea webhook。
运行：python3 relay/test_relay.py
"""
import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
import threading
import time
import types
import unittest


def load_relay(tmpdir):
    fake_nats = types.ModuleType("nats")

    class FakeNatsClient:
        def __init__(self):
            self.published = []
            self.connected = True
            self.flushes = 0

        async def publish(self, subject, data):
            self.published.append((subject, json.loads(data.decode())))

        async def flush(self):
            self.flushes += 1

    async def fake_connect(**kwargs):
        client = FakeNatsClient()
        fake_nats.last_client = client
        fake_nats.last_kwargs = kwargs
        return client

    fake_nats.connect = fake_connect
    import sys
    sys.modules["nats"] = fake_nats
    # 所有环境相关值在测试里显式注入，验证代码本身不含具体环境默认
    os.environ["BROADCAST_AUTHORS"] = "mayaoyao,sopyk"
    os.environ["RELAY_ID"] = "testhost.gitea-relay"
    os.environ["NOTIFY_SUBJECT_PREFIX"] = "evt.gitea.notify"
    os.environ["BROADCAST_SUBJECT"] = "evt.gitea.broadcast"
    spec = importlib.util.spec_from_file_location(
        "gitea_relay_under_test",
        os.path.join(os.path.dirname(__file__), "server.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.environ["MEMBERS_FILE"] = os.path.join(tmpdir, "members.json")
    os.environ["DEDUP_FILE"] = os.path.join(tmpdir, "state", "seen.json")
    with open(os.environ["MEMBERS_FILE"], "w", encoding="utf-8") as f:
        json.dump({"members": {
            "mayaoyao": {"host": "111", "id": "mayaoyao"},
            "madeng": {"host": "114", "id": "madeng"},
            "zhuazhua": {"host": "114", "id": "zhuazhua"},
        }}, f)
    mod.SEEN = mod.Seen(os.environ["DEDUP_FILE"])
    mod.load_members()
    return mod


class RelayBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mod = load_relay(self.tmpdir)
        self.sent = []
        self.publish_results = []
        self.mod.RELAY.loop = object()

        def fake_submit(ev):
            self.sent.append(ev)
            class F:
                def result(inner_self, timeout=None):
                    return self.publish_results.pop(0) if self.publish_results else []
            return F()

        self.mod.RELAY.submit_event = fake_submit

    def fake_http_post(self, payload, event="issue_comment", delivery="d1",
                       sign_secret="s3cr3t", server_secret="s3cr3t"):
        body = json.dumps(payload, ensure_ascii=False).encode()
        sig = hmac.new(sign_secret.encode(), body, hashlib.sha256).hexdigest()

        class FakeReq:
            def __init__(self):
                self.headers = {
                    "Content-Length": str(len(body)),
                    "X-Gitea-Signature": sig,
                    "X-Gitea-Delivery": delivery,
                    "X-Gitea-Event": event,
                }
                self.rfile = __import__("io").BytesIO(body)
                self.client_address = ("127.0.0.1", 1234)
                self.status = None
                self.body = b""

            def send_response(self, code):
                self.status = code

            def send_header(self, *a):
                pass

            def end_headers(self):
                pass

            def wfile_write(self, data):
                self.body += data

        req = FakeReq()
        req.wfile = types.SimpleNamespace(write=req.wfile_write)
        os.environ["WEBHOOK_SECRET"] = server_secret
        h = self.mod.Handler.__new__(self.mod.Handler)
        h.headers = req.headers
        h.rfile = req.rfile
        h.client_address = req.client_address
        h.send_response = req.send_response
        h.send_header = req.send_header
        h.end_headers = req.end_headers
        h.wfile = req.wfile
        h.path = "/gitea-hook"
        h.do_POST()
        return req.status, req.body

    def test_mentions_ignore_code_fences_and_inline_code(self):
        mentions = self.mod.extract_mentions(
            "请 @mayaoyao 看，`@madeng` 是代码，```\n@zhuazhua\n```\n再 @madeng"
        )
        self.assertEqual(mentions, ["mayaoyao", "madeng"])

    def test_comment_webhook_publishes_only_mapped_targets_and_dedupes(self):
        payload = {
            "repository": {"full_name": "songsir-group/AgentCouncil", "html_url": "http://g/repo"},
            "sender": {"login": "zhuazhua"},
            "issue": {"number": 134, "title": "test", "html_url": "http://g/134", "state": "open", "labels": []},
            "comment": {"html_url": "http://g/134#c1", "body": "@mayaoyao 请处理，@unknown 忽略"},
        }
        status, _ = self.fake_http_post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("mayaoyao", self.sent[0]["mentions"])
        status2, body2 = self.fake_http_post(payload)
        self.assertEqual(status2, 200)
        self.assertEqual(body2, b"duplicate")
        self.assertEqual(len(self.sent), 1)

    def test_publish_failure_returns_502_and_same_delivery_can_retry(self):
        payload = {
            "repository": {"full_name": "x", "html_url": "http://g/repo"},
            "sender": {"login": "zhuazhua"},
            "issue": {"number": 134, "title": "t", "html_url": "http://g/134", "state": "open", "labels": []},
            "comment": {"html_url": "http://g/134#c1", "body": "@mayaoyao 请处理"},
        }
        self.publish_results = [[("evt.gitea.notify.111.mayaoyao", False)]]
        status, body = self.fake_http_post(payload, delivery="d-fail")
        self.assertEqual(status, 502)
        self.assertEqual(body, b"publish-failed")
        self.assertEqual(len(self.sent), 1)
        # 第二次同 delivery（Gitea 重试）必须被允许投递，而不是被判 duplicate
        status2, _ = self.fake_http_post(payload, delivery="d-fail")
        self.assertEqual(status2, 200)
        self.assertEqual(len(self.sent), 2)
        # 成功后第三次才 duplicate
        status3, body3 = self.fake_http_post(payload, delivery="d-fail")
        self.assertEqual(status3, 200)
        self.assertEqual(body3, b"duplicate")
        self.assertEqual(len(self.sent), 2)

    def test_bad_signature_is_rejected(self):
        payload = {"repository": {}, "sender": {}, "action": "created"}
        status, _ = self.fake_http_post(payload, sign_secret="wrong")
        self.assertEqual(status, 401)

    def test_broadcast_detection(self):
        self.assertTrue(self.mod.is_broadcast({
            "kind": "issue", "action": "opened", "actor": "mayaoyao",
            "title": "【公告】测试", "labels": []}))
        self.assertFalse(self.mod.is_broadcast({
            "kind": "issue", "action": "opened", "actor": "madeng",
            "title": "普通", "labels": []}))

    def test_seen_state_roundtrip(self):
        path = os.path.join(self.tmpdir, "seen2.json")
        seen = self.mod.Seen(path)
        self.assertEqual(seen.begin("a"), "reserved")
        seen.commit("a")
        seen2 = self.mod.Seen(path)
        self.assertEqual(seen2.begin("a"), "seen")

    def test_failed_delivery_can_be_retried_with_same_uuid(self):
        seen = self.mod.Seen(os.path.join(self.tmpdir, "seen.json"))
        self.assertEqual(seen.begin("d-fail"), "reserved")
        seen.rollback("d-fail")
        self.assertEqual(seen.begin("d-fail"), "reserved")
        seen.commit("d-fail")
        self.assertEqual(seen.begin("d-fail"), "seen")

    def test_inflight_same_delivery_is_rejected_until_finished(self):
        seen = self.mod.Seen(os.path.join(self.tmpdir, "seen.json"))
        self.assertEqual(seen.begin("d-run"), "reserved")
        self.assertEqual(seen.begin("d-run"), "inflight")
        seen.commit("d-run")
        self.assertEqual(seen.begin("d-run"), "seen")


if __name__ == "__main__":
    unittest.main(verbosity=2)
