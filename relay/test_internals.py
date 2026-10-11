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

import json

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
        """payload 里没有 action_at 时必须落到兜底时间，不能抛异常。"""
        p = fixtures.issue_comment()
        self.assertNotIn("action_at", p, "fixture 本就不该有这个键")
        ev = self.norm(p, "issue_comment")
        self.assertRegex(ev["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                         "没有 action_at 时应回退到本地时钟")

    def test_explicit_action_at_is_preserved(self):
        """payload 真带了 action_at 就用它，不要覆盖。"""
        p = fixtures.issue_comment()
        p["action_at"] = "2026-01-02T03:04:05Z"
        self.assertEqual(self.norm(p, "issue_comment")["ts"], "2026-01-02T03:04:05Z")

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
        """无配置文件时必须和旧代码行为一致，否则 NAS 上已有部署会挂。

        _defaults() 里的 gitea 字样是**有意的兼容层**（纯 env 部署没有
        TOML，默认 provider 为空会导致升级即拒绝启动）。这条用例锁住它，
        同时也是「不该再加新东西进去」的提醒。
        """
        cfg = config_mod.load(path="/nonexistent", env=dict(GITEA_ENV))
        self.assertEqual(cfg.providers["gitea"].path, "/gitea-hook")
        self.assertEqual(cfg.providers["gitea"].event_header, "X-Gitea-Event")
        self.assertIsNone(cfg.providers["gitea"].event_type_header)
        self.assertEqual(cfg.dedup_file, "/data/seen.json")
        self.assertEqual(cfg.listen, "0.0.0.0:8090")
        self.assertEqual(cfg.publish_timeout_seconds, 3.0)
        self.assertEqual(cfg.notify_subject_prefix, "evt.gitea.notify")

    def test_defaults_only_contain_legacy_events(self):
        """兼容层里不该出现「只有新版才知道」的事件映射 ——
        旧版 EVENT_MAP 只有 4 条，多出来的就意味着默认值和历史行为不一致了。"""
        cfg = config_mod.load(path="/nonexistent", env=dict(GITEA_ENV))
        g = cfg.providers["gitea"]
        self.assertEqual(set(g.events),
                         {"issues", "issue_comment", "pull_request"})
        self.assertEqual(g.event_types, {},
                         "旧版没有细头表；默认值里加了会让纯 env 部署行为突变")


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


class ConfigTypeTest(unittest.TestCase):
    """F14：TOML 不做类型校验，写错类型曾导致功能静默永久失效。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = ('[nats]\nservers = ["nats://x:4222"]\nuser = "u"\npassword = "p"\n'
                     '[providers.gitea]\npath = "/h"\n'
                     '[providers.gitea.headers]\nevent = "X-E"\n'
                     '[providers.gitea.signature]\nsecret = "s"\n'
                     '[providers.gitea.events]\nissues = "issue"\n')

    def load_full(self, text, env=None):
        path = os.path.join(self.tmp, "full.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return config_mod.load(path=path, env=dict(GITEA_ENV) if env is None else env)

    def load_with(self, extra, env=None):
        path = os.path.join(self.tmp, "c.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.base + extra)
        return config_mod.load(path=path, env=dict(GITEA_ENV) if env is None else env)

    def test_broadcast_authors_as_bare_string_rejected(self):
        """写成 "alice"（漏了中括号）曾得到 {'a','l','i','c','e'}，
        全厅公告永久静默失效且无任何报错。"""
        with self.assertRaises(config_mod.ConfigError) as cm:
            self.load_with('\n[routing]\nbroadcast_authors = "alice"\n')
        self.assertIn("broadcast_authors", str(cm.exception))

    def test_broadcast_authors_comma_string_accepted(self):
        cfg = self.load_with('\n[routing]\nbroadcast_authors = "alice,bob"\n')
        self.assertEqual(cfg.broadcast_authors, {"alice", "bob"})

    def test_broadcast_authors_wrong_type_rejected(self):
        for bad in ("123", "true", '{"k":"v"}', "[1, 2]"):
            with self.subTest(bad=bad):
                with self.assertRaises(config_mod.ConfigError):
                    self.load_with(f'\n[routing]\nbroadcast_authors = {bad}\n')

    def test_servers_comma_string_accepted_and_ordered(self):
        """环境变量侧支持逗号分隔，文件侧曾不支持 —— 同一语义两条路径不一致。"""
        cfg = self.load_with('\n')
        path = os.path.join(self.tmp, "c2.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('[nats]\nservers = "nats://a:4222,nats://b:4222"\n'
                    'user = "u"\npassword = "p"\n'
                    '[providers.gitea]\npath = "/h"\n'
                    '[providers.gitea.headers]\nevent = "X-E"\n'
                    '[providers.gitea.signature]\nsecret = "s"\n'
                    '[providers.gitea.events]\nissues = "issue"\n')
        cfg = config_mod.load(path=path, env={})
        self.assertEqual(cfg.nats_servers, ["nats://a:4222", "nats://b:4222"],
                         "servers 必须保序（failover 依赖顺序）")

    def test_servers_wrong_type_rejected(self):
        path = os.path.join(self.tmp, "c3.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('[nats]\nservers = 4222\nuser = "u"\npassword = "p"\n'
                    '[providers.gitea]\npath = "/h"\n'
                    '[providers.gitea.headers]\nevent = "X-E"\n'
                    '[providers.gitea.signature]\nsecret = "s"\n'
                    '[providers.gitea.events]\nissues = "issue"\n')
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(path=path, env={})

    def test_zero_heartbeat_rejected(self):
        """0 会让 asyncio.sleep(0) 变成打 NATS 的忙循环。"""
        with self.assertRaises(config_mod.ConfigError) as cm:
            self.load_with('\n[service]\nheartbeat_seconds = 0\n')
        self.assertIn("heartbeat_seconds", str(cm.exception))

    def test_negative_and_bool_numbers_rejected(self):
        for bad in ("-1", "true"):
            with self.subTest(bad=bad):
                with self.assertRaises(config_mod.ConfigError):
                    self.load_with(f'\n[service]\nheartbeat_seconds = {bad}\n')

    def test_non_string_scalars_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            self.load_with('\n[service]\nclient_name = 123\n')

    def test_dedup_capacity_zero_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            self.load_with('\n[dedup]\ncapacity = 0\n')

    def test_enabled_must_be_boolean(self):
        """TOML 里 enabled = "false" 是 truthy 字符串，会让本该禁用的
        provider 保持启用 —— 静默接收它不该收的事件。"""
        toml = self.base.replace("[providers.gitea]\n",
                                 '[providers.gitea]\nenabled = "false"\n')
        with self.assertRaises(config_mod.ConfigError) as cm:
            self.load_full(toml)
        self.assertIn("enabled", str(cm.exception))

    def test_secret_must_be_string(self):
        """secret 写成数字必须被拒绝 —— 否则每次验签都失败。"""
        toml = self.base.replace('secret = "s"', "secret = 123")
        with self.assertRaises(config_mod.ConfigError) as cm:
            self.load_full(toml)
        self.assertIn("secret", str(cm.exception))

    def test_signature_algorithm_must_be_string(self):
        toml = self.base.replace('[providers.gitea.signature]\n',
                                 '[providers.gitea.signature]\nalgorithm = 123\n')
        with self.assertRaises(config_mod.ConfigError) as cm:
            self.load_full(toml)
        self.assertIn("algorithm", str(cm.exception))

    def test_event_types_value_must_be_known_kind(self):
        toml = self.base.replace('[providers.gitea.events]\nissues = "issue"\n',
                                 '[providers.gitea.events]\nissues = "issue"\n'
                                 '[providers.gitea.event_types]\nfoo = "abc"\n')
        with self.assertRaises(config_mod.ConfigError) as cm:
            self.load_full(toml)
        self.assertIn("未文档化的 kind", str(cm.exception))


class MalformedPayloadTest(unittest.TestCase):
    """payload 字段类型错时必须降级处理，不能抛异常。

    抛异常 = 500 = 平台用同一个 payload 无限重试，而这条通知永远发不出去。
    """

    def setUp(self):
        self.p, _ = gitea_provider()

    def norm(self, payload, kind="issue_comment"):
        return self.p.normalize(payload, kind, payload.get("action", ""))

    def test_wrong_scalar_types_do_not_crash(self):
        cases = {
            "labels 是数字": {"issue": {"number": 1, "labels": 5}},
            "labels 是字符串": {"issue": {"number": 1, "labels": "bug"}},
            "labels 元素是字符串": {"issue": {"number": 1, "labels": ["a", {"name": "b"}]}},
            "title 是数字": {"issue": {"number": 1, "title": 123}},
            "title 是 None": {"issue": {"number": 1, "title": None}},
            "title 是列表": {"issue": {"number": 1, "title": [1, 2]}},
            "comment.body 是数字": {"issue": {"number": 1},
                                    "comment": {"body": 123, "html_url": "u"}},
            "comment.body 是对象": {"issue": {"number": 1},
                                    "comment": {"body": {"a": 1}}},
            "comment.body 是列表": {"issue": {"number": 1},
                                    "comment": {"body": ["@bob"]}},
            "mentions.login 是数字": {"issue": {"number": 1},
                                      "comment": {"body": "", "mentions": [{"login": 123}]}},
            "state 是列表": {"issue": {"number": 1, "state": [1]}},
            "assignees 是数字": {"issue": {"number": 1, "assignees": 5}},
            "html_url 是数字": {"issue": {"number": 1, "html_url": 42}},
            "review.content 是数字": {"review": {"type": "x", "content": 5}},
            "review 是字符串": {"review": "not-a-dict"},
            "requested_reviewer 是字符串": {"requested_reviewer": "bob"},
            "repo.full_name 是数字": {"repository": {"full_name": 7}},
            "sender.login 是列表": {"sender": {"login": ["a"]}},
            "action 是数字": {"issue": {"number": 1}},
        }
        for name, payload in cases.items():
            with self.subTest(name):
                ev = self.norm(dict(payload))
                self.assertIn("kind", ev)

    def test_wrong_types_yield_sane_values(self):
        ev = self.norm({"issue": {"number": 1, "title": 123, "labels": 5,
                                  "assignees": "not-a-list", "html_url": 42}})
        self.assertEqual(ev["title"], "123")
        self.assertEqual(ev["labels"], [])
        self.assertEqual(ev["assignees"], [])
        self.assertEqual(ev["url"], "42")

    def test_none_becomes_empty_not_string_none(self):
        """None 必须变成空串而不是 "None" —— 后者会出现在展示界面上。"""
        ev = self.norm({"issue": {"number": 1, "title": None, "html_url": None},
                        "repository": {"full_name": None},
                        "sender": {"login": None}})
        self.assertEqual(ev["title"], "")
        self.assertEqual(ev["url"], "")
        self.assertEqual(ev["repo"], "")
        self.assertEqual(ev["actor"], "")

    def test_extract_mentions_tolerates_non_string(self):
        self.assertEqual(self.p.normalizer.__globals__["extract_mentions"](123), [])
        self.assertEqual(self.p.normalizer.__globals__["extract_mentions"](None), [])


class MembersSubjectSafetyTest(unittest.TestCase):
    """F15：host/id 会被编进 NATS subject，含 . 会投到别人的收件位。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        import json
        self.path = os.path.join(self.tmp, "m.json")

    def write(self, obj):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return self.path

    def test_dot_in_id_rejected(self):
        """evt.gitea.notify.h1.a.b 是**另一个** Agent 的收件位。"""
        with self.assertRaises(config_mod.ConfigError) as cm:
            config_mod.load_members(self.write(
                {"members": {"alice": {"host": "h1", "id": "a.b"}}}))
        self.assertIn("非法字符", str(cm.exception))

    def test_wildcards_rejected(self):
        for bad in ("*", ">", "a*", "a>b"):
            with self.subTest(bad=bad):
                with self.assertRaises(config_mod.ConfigError):
                    config_mod.load_members(self.write(
                        {"members": {"x": {"host": "h1", "id": bad}}}))

    def test_space_and_unicode_rejected(self):
        for bad in ("a b", "a\nb", "张三"):
            with self.subTest(bad=bad):
                with self.assertRaises(config_mod.ConfigError):
                    config_mod.load_members(self.write(
                        {"members": {"x": {"host": "h1", "id": bad}}}))

    def test_dangerous_host_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load_members(self.write(
                {"members": {"x": {"host": "h.1", "id": "ok"}}}))

    def test_valid_tokens_accepted(self):
        m = config_mod.load_members(self.write(
            {"members": {"alice": {"host": "h1", "id": "a-1_b"}}}))
        self.assertEqual(m["alice"], {"host": "h1", "id": "a-1_b"})

    def test_error_names_the_offending_login(self):
        with self.assertRaises(config_mod.ConfigError) as cm:
            config_mod.load_members(self.write({"members": {
                "good": {"host": "h1", "id": "good"},
                "bad": {"host": "h1", "id": "a.b"},
            }}))
        self.assertIn("bad", str(cm.exception))


class SeenCorruptionTest(unittest.TestCase):
    """F16：内容是合法 JSON 但类型不对时，去重会静默失效。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "seen.json")

    def test_string_payload_treated_as_empty(self):
        """手滑写成 "abc" 时，迭代出 {'a','b','c'}，
        begin(uuid) 永远 miss → relay 重启后把所有事件重发一遍。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('"abc"')
        s = core.Seen(self.path)
        self.assertEqual(s.begin("some-uuid"), "reserved")

    def test_int_array_treated_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        self.assertEqual(core.Seen(self.path).begin("x"), "reserved")

    def test_mixed_array_treated_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('["ok", 5, null]')
        self.assertEqual(core.Seen(self.path).begin("y"), "reserved")

    def test_valid_list_still_honoured(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('["a", "b"]')
        s = core.Seen(self.path)
        self.assertEqual(s.begin("a"), "seen")
        self.assertEqual(s.begin("b"), "seen")

    def test_empty_file_ok(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("")
        self.assertEqual(core.Seen(self.path).begin("z"), "reserved")


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

    def test_targets_are_deterministic(self):
        """排序后结果稳定 —— 同样的事件每次发出的 subject 顺序必须一致，
        否则排查问题时对不上日志。"""
        ev = {"assignees": ["zoe", "alice"], "mentions": ["madeng", "bob"]}
        runs = {tuple(core.extract_targets(ev, self.members)) for _ in range(20)}
        self.assertEqual(len(runs), 1, f"顺序不稳定: {runs}")

    def test_no_default_authors_in_code(self):
        """代码里不得内置任何具体账号 —— 扫全部产品模块，不只是 core。"""
        import glob
        import inspect
        here = os.path.dirname(os.path.abspath(__file__))
        offenders = []
        for path in glob.glob(os.path.join(here, "*.py")):
            base = os.path.basename(path)
            if base.startswith("test_") or base == "fixtures.py":
                continue
            with open(path, encoding="utf-8") as f:
                src = f.read()
            # 只看代码部分：docstring/注释里提到成员名不算
            for line in src.splitlines():
                code = line.split("#", 1)[0]
                if any(n in code for n in ("mayaoyao", "madeng", "zhuazhua", "sopyk")):
                    offenders.append(f"{base}: {line.strip()[:70]}")
        self.assertEqual(offenders, [], f"产品代码里出现了具体登录名: {offenders}")


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


class SubjectsForTest(unittest.TestCase):
    """Relay.subjects_for —— 决定「门铃发给谁」的接线层。

    之前这里零覆盖：E2E 测试里 fake relay 是拿测试自己塞进去的列表拼 subject，
    于是这条真正决定去向的代码一行都没跑过。
    """
    TOML = """
[nats]
servers = ["nats://x:4222"]
user = "u"
password = "p"
[routing]
members_file = "{members}"
notify_subject_prefix = "evt.gitea.notify"
broadcast_subject = "evt.gitea.broadcast"
broadcast_authors = ["mayaoyao"]
[providers.gitea]
path = "/gitea-hook"
[providers.gitea.headers]
event = "X-Gitea-Event"
delivery = "X-Gitea-Delivery"
signature = "X-Gitea-Signature"
[providers.gitea.signature]
secret = "s"
[providers.gitea.events]
issues = "issue"
issue_comment = "issue_comment"
"""

    def setUp(self):
        import app
        d = tempfile.mkdtemp()
        m = os.path.join(d, "m.json")
        with open(m, "w", encoding="utf-8") as f:
            json.dump({"members": {
                "alice": {"host": "h1", "id": "alice"},
                "madeng": {"host": "h2", "id": "madeng"},
            }}, f)
        p = os.path.join(d, "c.toml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.TOML.format(members=m))
        cfg = config_mod.load(path=p, env={})
        self.members = core.Members(config_mod.load_members(cfg.members_file))
        self.relay = app.Relay(cfg, self.members)

    def test_mention_produces_subject(self):
        """核心契约：evt.gitea.notify.<host>.<id>"""
        subs = self.relay.subjects_for({"mentions": ["alice"]})
        self.assertEqual(subs, ["evt.gitea.notify.h1.alice"])

    def test_custom_prefix_is_honored(self):
        self.relay.cfg.notify_subject_prefix = "acme.poke"
        self.assertEqual(self.relay.subjects_for({"mentions": ["alice"]}),
                         ["acme.poke.h1.alice"])

    def test_broadcast_subject_added_for_announcement(self):
        ev = {"kind": "issue", "action": "opened", "actor": "mayaoyao",
              "title": "【公告】上线", "labels": []}
        self.assertEqual(self.relay.subjects_for(ev), ["evt.gitea.broadcast"])

    def test_broadcast_and_notify_combined(self):
        ev = {"kind": "issue", "action": "opened", "actor": "mayaoyao",
              "title": "【公告】上线", "labels": [], "assignees": ["alice"]}
        self.assertEqual(sorted(self.relay.subjects_for(ev)),
                         ["evt.gitea.broadcast", "evt.gitea.notify.h1.alice"])

    def test_non_author_issue_is_not_broadcast(self):
        ev = {"kind": "issue", "action": "opened", "actor": "alice",
              "title": "【公告】上线", "labels": []}
        self.assertEqual(self.relay.subjects_for(ev), [])

    def test_unknown_login_yields_nothing(self):
        self.assertEqual(self.relay.subjects_for({"mentions": ["nobody"]}), [])

    def test_reviewers_and_assignees_both_routed(self):
        ev = {"assignees": ["alice"], "reviewers": ["madeng"]}
        self.assertEqual(sorted(self.relay.subjects_for(ev)),
                         ["evt.gitea.notify.h1.alice", "evt.gitea.notify.h2.madeng"])

    def test_same_recipient_not_published_twice(self):
        ev = {"assignees": ["alice"], "mentions": ["alice"], "reviewers": ["alice"]}
        self.assertEqual(self.relay.subjects_for(ev), ["evt.gitea.notify.h1.alice"])

    def test_nats_timeout_is_shorter_than_http_timeout(self):
        """NATS 侧预算必须小于 HTTP 层预算。

        否则 HTTP 必然先超时：它回 504、delivery 回滚、平台重试，
        而协程还在等 PONG，稍后 flush 成功 —— 消息其实早已上线，
        于是重复门铃。cancel() 撤不回已经写进 socket 的字节。
        """
        self.assertLess(self.relay._nats_timeout(),
                        self.relay.cfg.publish_timeout_seconds)

    def test_nats_timeout_never_zero(self):
        """publish_timeout_seconds 配得极小时，NATS 预算不能变成 0
        （flush(0) 会直接抛 BadTimeoutError）。"""
        self.relay.cfg.publish_timeout_seconds = 0.01
        self.assertGreater(self.relay._nats_timeout(), 0)

    def test_flush_receives_the_timeout(self):
        """flush 必须显式传 timeout —— 不传就用 nats-py 的 10s 默认值，
        比 HTTP 层慢一大截，重复投递窗口被放大到最大。"""
        import asyncio
        seen = {}

        class NC:
            is_connected = True

            async def publish(self, s, d):
                pass

            async def flush(self, timeout=None):
                seen["timeout"] = timeout

        self.relay.nc = NC()
        asyncio.run(self.relay._publish("s", {"k": 1}))
        self.assertIsNotNone(seen.get("timeout"),
                             "flush 没拿到 timeout —— 会退回 10s 默认值")
        self.assertLessEqual(seen["timeout"], self.relay.cfg.publish_timeout_seconds)

    def test_empty_event_yields_nothing(self):
        self.assertEqual(self.relay.subjects_for({}), [])


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