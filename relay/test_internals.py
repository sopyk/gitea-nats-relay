#!/usr/bin/env python3
"""provider / 核心逻辑单元测试。

覆盖原来零覆盖的几段：payload 归一化、@提及边界、事件映射、成员映射校验、
收件人解析、公告判定、配置加载与校验。

运行：python3 test_internals.py
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.modules.setdefault("nats", types.ModuleType("nats"))

import config as config_mod
import core
import fixtures
import providers as providers_mod

GITEA_ENV = {
    "NATS_SERVERS": "nats://h:4222",
    "NATS_USER": "u",
    "NATS_PASSWORD": "p",
    "WEBHOOK_SECRET": "s3cr3t",
}


def gitea_provider(extra_toml=None):
    """加载一份 Gitea provider 配置（默认走环境变量路径）。"""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "c.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(extra_toml or "")
    cfg = config_mod.load(path=path, env=dict(GITEA_ENV))
    return providers_mod.build_registry(cfg)["/gitea-hook"], cfg


class MentionTest(unittest.TestCase):
    """@提及的边界判定 —— 旧实现用 \\b / \\w，汉字被当成单词字符导致漏匹配。"""

    def test_ascii_with_spaces(self):
        self.assertEqual(providers_mod.extract_mentions("请 @bob 看，@amy 处理"),
                         ["bob", "amy"])

    def test_chinese_without_spaces(self):
        """中文正文最常见的写法 —— 旧实现在这里全部漏掉。"""
        for text in ("请@bob看", "@bob请尽快看", "@bob，请跟进", "@bob。", "辛苦@bob了"):
            self.assertEqual(providers_mod.extract_mentions(text), ["bob"],
                             f"漏匹配: {text!r}")

    def test_code_is_stripped(self):
        text = "请 @bob 看，`@amy` 是代码，```\n@zhuazhua\n```\n再 @mayaoyao"
        self.assertEqual(providers_mod.extract_mentions(text), ["bob", "mayaoyao"])

    def test_emails_are_not_mentions(self):
        self.assertEqual(providers_mod.extract_mentions("联系 user@example.com"), [])

    def test_paths_are_not_mentions(self):
        self.assertEqual(providers_mod.extract_mentions("见 src/@file 目录"), [])

    def test_trailing_punctuation_not_swallowed(self):
        self.assertEqual(providers_mod.extract_mentions("找 @bob.a 看看"), [])
        self.assertEqual(providers_mod.extract_mentions("@bob 的任务"), ["bob"])


class NormalizeTest(unittest.TestCase):
    """payload 归一化。"""

    def setUp(self):
        self.p, self.cfg = gitea_provider()

    def norm(self, payload, kind, action=""):
        return self.p.normalize(payload, kind, action or payload.get("action", ""))

    def test_ts_is_never_empty(self):
        """Gitea payload 没有 action_at，旧代码恒返回空串。"""
        ev = self.norm(fixtures.issue_comment(), "issue_comment")
        self.assertTrue(ev["ts"], "ts 不能为空")
        self.assertRegex(ev["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_ts_absent_key_is_tolerated(self):
        ev = self.norm(fixtures.issue_comment(), "issue_comment")
        self.assertIsNotNone(ev["ts"])

    def test_issue_comment_fields(self):
        ev = self.norm(fixtures.issue_comment(), "issue_comment")
        self.assertEqual(ev["kind"], "issue_comment")
        self.assertEqual(ev["action"], "created")
        self.assertEqual(ev["repo"], "org/repo")
        self.assertEqual(ev["actor"], "you")
        self.assertEqual(ev["number"], 134)
        self.assertEqual(ev["state"], "open")
        self.assertEqual(ev["assignees"], ["mayaoyao"])
        self.assertEqual(ev["labels"], ["bug"])
        self.assertIn("#issuecomment-42", ev["comment_url"])
        self.assertEqual(ev["mentions"], ["madeng", "mayaoyao"])

    def test_review_reads_content_not_body(self):
        """Gitea ReviewPayload 是 {"type","content"}。旧代码读 body → 提及全丢。"""
        ev = self.norm(fixtures.review_approved(), "review")
        self.assertEqual(ev["review_state"], "pull_request_review_approved")
        self.assertEqual(ev["mentions"], ["mayaoyao"])
        self.assertEqual(ev["kind"], "review")

    def test_review_rejected_and_comment(self):
        ev = self.norm(fixtures.review_rejected(), "review")
        self.assertEqual(ev["review_state"], "pull_request_review_rejected")
        self.assertEqual(ev["mentions"], ["madeng"])

        ev = self.norm(fixtures.review_comment(), "review")
        self.assertEqual(ev["review_state"], "pull_request_review_comment")
        # 正文里的 @ 是行内码，不应被抽出来
        self.assertEqual(ev["mentions"], [])

    def test_reviewers_from_pull_request(self):
        ev = self.norm(fixtures.pull_request_event(), "pull_request")
        self.assertEqual(ev["reviewers"], ["madeng"])

    def test_requested_reviewer_singular_is_captured(self):
        """review_requested 时真正被请求的人在顶层单数字段里，PR 上是空的。"""
        ev = self.norm(fixtures.review_requested("madeng"), "review")
        self.assertEqual(ev["reviewers"], ["madeng"])

    def test_requested_reviewer_deduped(self):
        """同时出现在单数和复数里也不该重复。"""
        p = fixtures.review_requested("madeng")
        p["pull_request"]["requested_reviewers"] = [{"login": "madeng"}]
        ev = self.norm(p, "review")
        self.assertEqual(ev["reviewers"], ["madeng"])

    def test_is_pr_flagged_for_pr_comment(self):
        ev = self.norm(fixtures.issue_comment(is_pull=True), "issue_comment")
        self.assertTrue(ev.get("is_pr"))

    def test_body_never_included(self):
        """门铃不能带正文 —— 这是「被打穿也只影响伪造空门铃」的前提。"""
        secret = "TOP_SECRET_COMMENT_BODY"
        ev = self.norm(fixtures.issue_comment(body=f"@bob {secret}"), "issue_comment")
        import json
        self.assertNotIn(secret, json.dumps(ev, ensure_ascii=False))

    def test_title_truncated(self):
        ev = self.norm(fixtures.issue_comment(issue=fixtures._issue(title="标" * 400)),
                       "issue_comment")
        self.assertEqual(len(ev["title"]), providers_mod.MAX_TITLE_CHARS)

    def test_malformed_payload_does_not_crash(self):
        for payload in ({}, {"repository": None, "sender": None},
                        {"issue": None, "comment": {}},
                        {"labels": "not-a-list"} and {"issue": {"labels": ["str"]}}):
            ev = self.norm(payload, "issue_comment")
            self.assertIn("kind", ev)


class EventMappingTest(unittest.TestCase):
    """双事件头映射。"""

    TOML = """
[providers.gitea]
path = "/gitea-hook"
[providers.gitea.headers]
event = "X-Gitea-Event"
event_type = "X-Gitea-Event-Type"
delivery = "X-Gitea-Delivery"
signature = "X-Gitea-Signature"
[providers.gitea.signature]
algorithm = "hmac-sha256"
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

    def setUp(self):
        self.p, self.cfg = gitea_provider(self.TOML)

    def kind(self, event, event_type=None):
        h = {"X-Gitea-Event": event}
        if event_type:
            h["X-Gitea-Event-Type"] = event_type
        return self.p.resolve_kind(h)

    def test_review_events_survive_in_coarse_header(self):
        """评审三事件在粗头里本就独立，不能因为「粗头是组名」就删掉。"""
        self.assertEqual(self.kind("pull_request_approved"), "review")
        self.assertEqual(self.kind("pull_request_rejected"), "review")
        self.assertEqual(self.kind("pull_request_comment"), "review")

    def test_review_request_needs_fine_header(self):
        self.assertEqual(self.kind("pull_request", "pull_request_review_request"), "review")

    def test_sync_is_distinguishable_from_review_request(self):
        """这正是旧设计做不到的：两者粗头都是 pull_request。"""
        self.assertEqual(self.kind("pull_request", "pull_request_sync"), "pull_request")
        self.assertNotEqual(self.kind("pull_request", "pull_request_review_request"),
                            self.kind("pull_request", "pull_request_sync"))

    def test_unmapped_event_returns_none(self):
        self.assertIsNone(self.kind("push"))
        self.assertIsNone(self.kind(""))

    def test_fine_table_takes_precedence(self):
        """同一事件两张表都有且值不同时，细头赢。"""
        p, _ = gitea_provider(self.TOML.replace(
            '[providers.gitea.event_types]',
            '[providers.gitea.event_types]\npull_request = "review"'))
        self.assertEqual(p.cfg.events["pull_request"], "pull_request")
        self.assertEqual(p.cfg.resolve_kind("pull_request", "pull_request"), "review")

    def test_empty_event_type_falls_back_to_coarse(self):
        """没有细头时（空值），不该去查细头表。"""
        p, _ = gitea_provider(self.TOML)
        self.assertEqual(p.cfg.resolve_kind("pull_request", ""), "pull_request")

    def test_single_header_platform_falls_back(self):
        """GitHub 这类只有粗头的平台：细头表不生效，退化成单表匹配。"""
        toml = """
[providers.gitea]
path = "/gitea-hook"
[providers.gitea.headers]
event = "X-Gitea-Event"
[providers.gitea.events]
issues = "issue"
"""
        p, _ = gitea_provider(toml)
        self.assertIsNone(p.cfg.event_type_header)
        self.assertEqual(p.resolve_kind({"X-Gitea-Event": "issues"}), "issue")


MIN_PROVIDER = ('[providers.gitea]\npath = "/gitea-hook"\n'
                '[providers.gitea.headers]\nevent = "X-Gitea-Event"\n'
                '[providers.gitea.signature]\nsecret = "s"\n'
                '[providers.gitea.events]\nissues = "issue"\n')


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def write(self, text):
        path = os.path.join(self.tmp, "c.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_missing_required_fails_loudly(self):
        for env in ({}, {**GITEA_ENV, "NATS_SERVERS": ""}):
            with self.assertRaises(config_mod.ConfigError):
                config_mod.load(path="/nonexistent", env=env)

    def test_env_overrides_file(self):
        path = self.write('[nats]\nservers = ["nats://from-file:4222"]\n'
                          'user = "file-user"\npassword = "file-pw"\n'
                          + MIN_PROVIDER)
        cfg = config_mod.load(path=path, env=dict(GITEA_ENV))
        self.assertEqual(cfg.nats_servers, ["nats://h:4222"])
        self.assertEqual(cfg.nats_user, "u")

    def test_file_used_when_no_env(self):
        path = self.write(
            '[nats]\nservers = ["nats://from-file:4222"]\nuser = "fu"\npassword = "fp"\n'
            '[providers.gitea]\npath = "/gitea-hook"\n'
            '[providers.gitea.headers]\nevent = "X-Gitea-Event"\n'
            '[providers.gitea.signature]\nsecret = "from-file-secret"\n'
            '[providers.gitea.events]\nissues = "issue"\n')
        cfg = config_mod.load(path=path, env={})
        self.assertEqual(cfg.nats_servers, ["nats://from-file:4222"])
        self.assertEqual(cfg.providers["gitea"].signature.secret, "from-file-secret")

    def test_incomplete_file_fails_loudly(self):
        """只写 [nats] 不写 providers 必须报错，而不是静默退回内置默认 ——
        宁可起不来，也不要在用户以为配好了的情况下悄悄换掉事件映射。"""
        path = self.write('[nats]\nservers = ["nats://x:4222"]\n')
        with self.assertRaises(config_mod.ConfigError) as cm:
            config_mod.load(path=path, env=dict(GITEA_ENV))
        self.assertIn("provider", str(cm.exception))

    def test_unknown_key_rejected(self):
        """表名拼错不报错是最阴的坑 —— 必须拦住。"""
        path = self.write('[nets]\nservers = []\n')
        with self.assertRaises(config_mod.ConfigError) as cm:
            config_mod.load(path=path, env=dict(GITEA_ENV))
        self.assertIn("未知配置项", str(cm.exception))

    def test_misspelled_event_types_table_rejected(self):
        toml = ('[providers.gitea]\npath = "/h"\n'
                '[providers.gitea.headers]\nevent = "X-E"\nevent_type = "X-ET"\n'
                '[providers.gitea.signature]\nsecret = "s"\n'
                '[providers.gitea.events]\nissues = "issue"\n'
                '[providers.gitea.event_type]\npull_request_review_request = "review"\n')
        with self.assertRaises(config_mod.ConfigError) as cm:
            config_mod.load(path=self.write(toml), env=dict(GITEA_ENV))
        self.assertIn("未知配置项", str(cm.exception))

    def test_event_types_without_header_rejected(self):
        toml = ('[providers.gitea]\npath = "/h"\n'
                '[providers.gitea.headers]\nevent = "X-E"\n'
                '[providers.gitea.signature]\nsecret = "s"\n'
                '[providers.gitea.events]\nissues = "issue"\n'
                '[providers.gitea.event_types]\nfoo = "issue"\n')
        with self.assertRaises(config_mod.ConfigError) as cm:
            config_mod.load(path=self.write(toml), env=dict(GITEA_ENV))
        self.assertIn("event_type", str(cm.exception))

    def test_invalid_kind_rejected(self):
        toml = ('[providers.gitea]\npath = "/h"\n'
                '[providers.gitea.headers]\nevent = "X-E"\n'
                '[providers.gitea.signature]\nsecret = "s"\n'
                '[providers.gitea.events]\nissues = "issues"\n')
        with self.assertRaises(config_mod.ConfigError) as cm:
            config_mod.load(path=self.write(toml), env=dict(GITEA_ENV))
        self.assertIn("未文档化的 kind", str(cm.exception))

    def test_bad_signature_algorithm_rejected(self):
        toml = ('[providers.gitea]\npath = "/h"\n'
                '[providers.gitea.headers]\nevent = "X-E"\n'
                '[providers.gitea.signature]\nalgorithm = "md5"\nsecret = "s"\n'
                '[providers.gitea.events]\nissues = "issue"\n')
        with self.assertRaises(config_mod.ConfigError) as cm:
            config_mod.load(path=self.write(toml), env=dict(GITEA_ENV))
        self.assertIn("验签算法", str(cm.exception))

    def test_conflicting_tables_warn(self):
        """同一个键在两张表里值不同 → 告警点名，否则改动静默不生效。"""
        toml = ('[providers.gitea]\npath = "/h"\n'
                '[providers.gitea.headers]\nevent = "X-E"\nevent_type = "X-ET"\n'
                '[providers.gitea.signature]\nsecret = "s"\n'
                '[providers.gitea.events]\npull_request = "pull_request"\n'
                '[providers.gitea.event_types]\npull_request = "review"\n')
        msgs = []
        config_mod.load(path=self.write(toml), env=dict(GITEA_ENV), warn=msgs.append)
        self.assertTrue(any("event_types" in m for m in msgs), msgs)

    def test_event_types_only_delta_does_not_warn(self):
        """只在细头表出现的键是正常用法（delta 条目），不该告警刷屏。"""
        toml = ('[providers.gitea]\npath = "/h"\n'
                '[providers.gitea.headers]\nevent = "X-E"\nevent_type = "X-ET"\n'
                '[providers.gitea.signature]\nsecret = "s"\n'
                '[providers.gitea.events]\npull_request = "pull_request"\n'
                '[providers.gitea.event_types]\npull_request_review_request = "review"\n')
        msgs = []
        config_mod.load(path=self.write(toml), env=dict(GITEA_ENV), warn=msgs.append)
        self.assertEqual(msgs, [])

    def test_defaults_match_legacy_behaviour(self):
        """无配置文件时必须和旧代码行为一致，否则 NAS 上已有部署会挂。"""
        cfg = config_mod.load(path="/nonexistent", env=dict(GITEA_ENV))
        self.assertEqual(cfg.providers["gitea"].path, "/gitea-hook")
        self.assertEqual(cfg.providers["gitea"].event_header, "X-Gitea-Event")
        self.assertIsNone(cfg.providers["gitea"].event_type_header)
        self.assertEqual(cfg.dedup_file, "/data/seen.json")
        self.assertEqual(cfg.listen, "0.0.0.0:8090")
        self.assertEqual(cfg.publish_timeout_seconds, 3.0)
        self.assertEqual(cfg.notify_subject_prefix, "evt.gitea.notify")


class MembersTest(unittest.TestCase):
    """成员映射的逐条校验 —— 历史上这里是裸 m.get()，一个脏值全员 500。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def write(self, obj):
        import json
        path = os.path.join(self.tmp, "members.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return path

    def test_valid_mapping(self):
        m = config_mod.load_members(self.write(
            {"members": {"alice": {"host": "h1", "id": "alice"}}}))
        self.assertEqual(m["alice"], {"host": "h1", "id": "alice"})

    def test_flat_format_supported(self):
        m = config_mod.load_members(self.write(
            {"alice": {"host": "h1", "id": "alice"}}))
        self.assertEqual(m["alice"], {"host": "h1", "id": "alice"})

    def test_missing_file_is_empty(self):
        self.assertEqual(config_mod.load_members("/nonexistent/m.json"), {})

    def test_string_value_rejected_with_clear_message(self):
        with self.assertRaises(config_mod.ConfigError) as cm:
            config_mod.load_members(self.write({"members": {"alice": "h1-114"}}))
        msg = str(cm.exception)
        self.assertIn("alice", msg)
        self.assertIn("host", msg)

    def test_missing_host_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load_members(self.write({"members": {"alice": {"id": "a"}}}))

    def test_bad_json_rejected(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load_members(path)

    def test_one_bad_entry_rejects_whole_file(self):
        """宁可整个拒绝也不半吊子加载 —— 静默跳过会让人以为配好了。"""
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load_members(self.write({"members": {
                "good": {"host": "h1", "id": "good"},
                "bad": "oops",
            }}))


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.members = core.Members({
            "alice": {"host": "h1", "id": "alice"},
            "bob": {"host": "h2", "id": "bob"},
            "same": {"host": "h1", "id": "alice"},
        })

    def test_targets_from_all_sources(self):
        ev = {"assignees": ["alice"], "mentions": ["bob"], "reviewers": ["alice"]}
        self.assertEqual(sorted(core.extract_targets(ev, self.members)),
                         [("h1", "alice"), ("h2", "bob")])

    def test_same_host_id_deduped(self):
        """两个登录名指向同一收件位时只发一次。"""
        ev = {"assignees": ["alice"], "mentions": ["same"]}
        self.assertEqual(core.extract_targets(ev, self.members), [("h1", "alice")])

    def test_unknown_login_not_routed(self):
        self.assertEqual(core.extract_targets({"mentions": ["nobody"]}, self.members), [])

    def test_broadcast_authors_only(self):
        self.assertTrue(core.is_broadcast(
            {"kind": "issue", "action": "opened", "actor": "mayaoyao",
             "title": "【公告】上线", "labels": []}, {"mayaoyao"}))
        self.assertTrue(core.is_broadcast(
            {"kind": "issue", "action": "opened", "actor": "mayaoyao",
             "title": "普通标题", "labels": [{"name": "公告"}]}, {"mayaoyao"}))
        self.assertFalse(core.is_broadcast(
            {"kind": "issue", "action": "opened", "actor": "someone",
             "title": "【公告】上线", "labels": []}, {"mayaoyao"}))
        self.assertFalse(core.is_broadcast(
            {"kind": "issue_comment", "action": "created", "actor": "mayaoyao",
             "title": "【公告】", "labels": []}, {"mayaoyao"}))

    def test_no_default_authors_in_code(self):
        """代码里不得内置任何具体账号。"""
        import inspect
        src = inspect.getsource(core)
        self.assertNotIn("mayaoyao", src)


class SeenTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "state", "seen.json")
        self.seen = core.Seen(self.path)

    def test_lifecycle(self):
        self.assertEqual(self.seen.begin("a"), "reserved")
        self.assertEqual(self.seen.begin("a"), "inflight")
        self.seen.commit("a")
        self.assertEqual(self.seen.begin("a"), "seen")

    def test_rollback_allows_retry(self):
        self.seen.begin("d")
        self.seen.rollback("d")
        self.assertEqual(self.seen.begin("d"), "reserved")

    def test_missing_delivery_flagged(self):
        """没有 delivery 头必须显式拒绝，否则去重形同虚设。"""
        self.assertEqual(self.seen.begin(""), "missing")
        self.assertEqual(self.seen.begin(None), "missing")

    def test_persists_across_restart(self):
        self.seen.begin("k")
        self.seen.commit("k")
        self.assertEqual(core.Seen(self.path).begin("k"), "seen")

    def test_lru_eviction(self):
        s = core.Seen(self.path, cap=3)
        for k in "abcd":
            s.begin(k)
            s.commit(k)
        self.assertEqual(s.begin("a"), "reserved")  # 最早的被淘汰了
        self.assertEqual(s.begin("d"), "seen")

    def test_corrupt_file_does_not_crash(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(core.Seen(self.path).begin("x"), "reserved")


class SignatureTest(unittest.TestCase):
    def test_valid_and_invalid(self):
        import hashlib
        import hmac as hmac_mod
        p, _ = gitea_provider()
        body = b'{"a":1}'
        sig = hmac_mod.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
        self.assertTrue(p.verify(body, {"X-Gitea-Signature": sig}))
        self.assertFalse(p.verify(body, {"X-Gitea-Signature": "wrong"}))
        self.assertFalse(p.verify(body, {}))
        self.assertFalse(p.verify(b'{"a":2}', {"X-Gitea-Signature": sig}))


class RegistryTest(unittest.TestCase):
    def test_duplicate_path_rejected(self):
        toml = ('[providers.gitea]\npath = "/same"\nenabled = true\n'
                '[providers.gitea.headers]\nevent = "X-E"\n'
                '[providers.gitea.signature]\nsecret = "s"\n'
                '[providers.gitea.events]\nissues = "issue"\n'
                '[providers.github]\npath = "/same"\n'
                '[providers.github.headers]\nevent = "X-GHE"\n'
                '[providers.github.signature]\nsecret = "s"\n'
                '[providers.github.events]\npull_request = "pull_request"\n')
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "c.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(toml)
        cfg = config_mod.load(path=path, env=dict(GITEA_ENV))
        with self.assertRaises(ValueError):
            providers_mod.build_registry(cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)