#!/usr/bin/env python3
"""部署产物一致性测试（标准库；PyYAML 存在时追加 compose 结构校验）。

这个项目最贵的失败模式不是代码挂掉，而是**文档、配置模板、实际代码互相漂移**：
README 说 A、示例 TOML 写 B、server.py 读 C，���且没有任何报错。
下面的用例把三者钉在一起。

运行：python3 test_deploy.py
"""
import json
import os
import re
import subprocess
import sys
import tomllib
import unittest

try:
    import importlib.util
    HAS_YAML = importlib.util.find_spec("yaml") is not None
except Exception:  # noqa
    HAS_YAML = False

ROOT = os.path.dirname(os.path.abspath(__file__))
TOML_EXAMPLE = os.path.join(ROOT, "config", "webhook2nats.example.toml")
ENV_EXAMPLE = os.path.join(ROOT, "relay.env.example")
COMPOSE_RUN = os.path.join(ROOT, "docker-compose.yml")
COMPOSE_BUILD = os.path.join(ROOT, "docker-compose.build.yml")
README = os.path.join(ROOT, "README.md")
SERVER = os.path.join(ROOT, "relay", "server.py")

# README「消息格式」一节承诺的统一事件类型。改了这里等于改出站契约，
# 所有已订阅的 Agent 都要跟着改 —— 必须是有意识的动作。
DOCUMENTED_KINDS = {"issue", "issue_comment", "pull_request", "review"}


def strip_comments(text):
    """去掉整行注释与行尾 # 注释，保留结构。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        # 只在引号外找 #，避免砍掉值里的 # 号
        if "#" in line:
            in_q = None
            for i, ch in enumerate(line):
                if in_q:
                    if ch == in_q:
                        in_q = None
                elif ch in "\"'":
                    in_q = ch
                elif ch == "#":
                    line = line[:i]
                    break
        if line.strip():
            out.append(line.rstrip())
    return "\n".join(out)


def parse_toml_sections(text):
    """把 TOML 解析成 {section_path: {key: value}}，跳过注释。

    用来在「不依赖文件在磁盘上」的前提下比较两段 TOML。
    """
    sections, cur = {}, None
    for line in strip_comments(text).splitlines():
        m = re.fullmatch(r"\[([^\]]+)\]", line.strip())
        if m:
            cur = sections.setdefault(m.group(1), {})
            continue
        if "=" not in line or cur is None:
            continue
        k, _, v = line.partition("=")
        cur[k.strip()] = v.strip()
    return sections


def extract_readme_toml_block(anchor):
    """取 README 里紧跟 anchor 的第一个 ```toml 代码块。"""
    with open(README, encoding="utf-8") as f:
        text = f.read()
    i = text.index(anchor)
    start = text.index("```toml", i)
    start = text.index("\n", start) + 1
    end = text.index("```", start)
    return text[start:end]


class ExampleConfigTest(unittest.TestCase):
    """示例配置模板本身的自洽性。"""

    def setUp(self):
        with open(TOML_EXAMPLE, "rb") as f:
            self.cfg = tomllib.load(f)

    def test_parses_and_has_expected_sections(self):
        for sec in ("service", "nats", "routing", "dedup", "providers"):
            self.assertIn(sec, self.cfg, f"缺少 [{sec}] 段")

    def test_no_real_environment_values(self):
        """模板里所有敏感值都必须是占位符，不能是真东西。"""
        checks = [
            (self.cfg["nats"]["servers"], "<nats-host>"),
            (self.cfg["nats"]["user"], "<nats-user>"),
            (self.cfg["nats"]["password"], "<nats-password>"),
            (self.cfg["providers"]["gitea"]["signature"]["secret"], "<"),
        ]
        for value, marker in checks:
            self.assertIn(marker, str(value),
                          f"{value!r} 不含占位符标记 {marker!r}，疑似真实环境值")
        for author in self.cfg["routing"]["broadcast_authors"]:
            self.assertTrue(author.startswith("<"),
                            f"broadcast_authors 里出现疑似真实登录名: {author!r}")

    def test_provider_declares_both_event_headers(self):
        h = self.cfg["providers"]["gitea"]["headers"]
        self.assertEqual(h["event"], "X-Gitea-Event")
        self.assertEqual(h["event_type"], "X-Gitea-Event-Type")
        self.assertEqual(h["delivery"], "X-Gitea-Delivery")
        self.assertEqual(h["signature"], "X-Gitea-Signature")

    def test_provider_has_both_mapping_tables(self):
        p = self.cfg["providers"]["gitea"]
        self.assertIn("events", p, "缺 [providers.gitea.events] 粗头映射表")
        self.assertIn("event_types", p, "缺 [providers.gitea.event_types] 细头映射表")

    def test_all_mapped_kinds_are_documented(self):
        """映射出来的 kind 必须落在 README 承诺的集合里，否则下游会收到未知类型。"""
        p = self.cfg["providers"]["gitea"]
        for table in ("events", "event_types"):
            for key, kind in p[table].items():
                self.assertIn(kind, DOCUMENTED_KINDS,
                              f"[{table}] {key} 映射到未文档化的 kind {kind!r}")

    def test_review_request_is_routed_distinctly(self):
        """「请求评审」在粗头里是 pull_request，必须靠细头表单独救回来。"""
        p = self.cfg["providers"]["gitea"]
        self.assertIn("pull_request_review_request", p["event_types"],
                      "event_types 缺 pull_request_review_request —— "
                      "README 承诺的「请求评审」路由将无法实现")
        self.assertNotIn("pull_request_review_request", p["events"],
                         "细头条目不该同时出现在粗头表里")

    def test_review_events_use_real_header_values(self):
        """Gitea 从不发出 header 值 pull_request_review，别照抄分组键。"""
        p = self.cfg["providers"]["gitea"]
        for bogus in ("pull_request_review",):
            self.assertNotIn(bogus, p["events"],
                             f"[events] 出现不存在的 header 值 {bogus!r}，会静默丢事件")
        for real in ("pull_request_approved", "pull_request_rejected", "pull_request_comment"):
            self.assertIn(real, p["events"], f"[events] 缺 {real}")


class ReadmeConsistencyTest(unittest.TestCase):
    """README 与实际产物的一致性。"""

    def test_relative_links_resolve(self):
        with open(README, encoding="utf-8") as f:
            text = f.read()
        targets = re.findall(r"\]\((?!https?:)([^)#]+)\)", text)
        self.assertTrue(targets, "README 里一个相对链接都没有，可疑")
        for t in targets:
            self.assertTrue(os.path.exists(os.path.join(ROOT, t)),
                            f"README 引用了不存在的文件: {t}")

    def test_provider_block_matches_example_config(self):
        """README 里的 provider 示例必须和 config/webhook2nats.example.toml 一致。

        这是最容易漂移的一处：改了模板忘了同步 README，读者就会照着
        一个不存在的配置去部署。
        """
        block = extract_readme_toml_block("## 通用模型")
        readme_secs = parse_toml_sections(block)
        with open(TOML_EXAMPLE, encoding="utf-8") as f:
            file_secs = parse_toml_sections(f.read())

        for section in ("providers.gitea.headers",
                        "providers.gitea.signature",
                        "providers.gitea.events",
                        "providers.gitea.event_types"):
            self.assertIn(section, readme_secs, f"README 示例缺 {section}")
            self.assertIn(section, file_secs, f"示例配置缺 {section}")
            self.assertEqual(
                readme_secs[section], file_secs[section],
                f"section {section} 在 README 和示例配置之间不一致")

    def test_documented_kind_set_matches_example(self):
        with open(README, encoding="utf-8") as f:
            text = f.read()
        with open(TOML_EXAMPLE, "rb") as f:
            cfg = tomllib.load(f)
        used = set(cfg["providers"]["gitea"]["events"].values())
        used |= set(cfg["providers"]["gitea"]["event_types"].values())
        # README 必须把用到的每个 kind 都写进那张表
        for kind in used:
            self.assertIn(f"`{kind}`", text, f"README 未文档化 kind {kind!r}")

    def test_both_compose_files_are_documented(self):
        with open(README, encoding="utf-8") as f:
            text = f.read()
        for f in ("docker-compose.yml", "docker-compose.build.yml"):
            self.assertIn(f, text, f"README 没提到 {f}")


class ComposeTest(unittest.TestCase):
    """两份 compose 必须结构一致，只有镜像来源不同。"""

    def _lines(self, path):
        return [l.rstrip() for l in strip_comments(open(path, encoding="utf-8").read()).splitlines()]

    def test_files_exist(self):
        for p in (COMPOSE_RUN, COMPOSE_BUILD):
            self.assertTrue(os.path.exists(p), f"缺少 {p}")

    def test_run_file_uses_prebuilt_image_without_build(self):
        text = open(COMPOSE_RUN, encoding="utf-8").read()
        self.assertNotIn("build:", text, "docker-compose.yml 不该带 build")
        self.assertIn("image: ghcr.io/sopyk/webhook2nats", text)
        self.assertIn("sopyk/webhook2nats", text, "应提到 Docker Hub 作为备选")

    def test_build_file_builds_locally(self):
        text = open(COMPOSE_BUILD, encoding="utf-8").read()
        self.assertIn("build: ./relay", text, "docker-compose.build.yml 应从 ./relay 构建")
        self.assertIn("image: webhook2nats:local", text)

    def test_build_context_matches_dockerfile_location(self):
        df_path = os.path.join(ROOT, "relay", "Dockerfile")
        self.assertTrue(os.path.exists(df_path))
        df = open(df_path, encoding="utf-8").read()
        # 模块化之后是多个 .py，COPY 必须跟着改成 *.py，
        # 否则镜像里只有一个文件，启动时 import 就炸。
        self.assertIn("COPY *.py /app/", df,
                      "Dockerfile 还在只 COPY 单个文件，模块化后会启动失败")
        self.assertIn("app.py", df.split("CMD")[-1],
                      "容器入口应指向 app.py")

    def test_every_module_is_copied(self):
        """镜像要能 import 到每一个模块，别漏了新增的。"""
        df = open(os.path.join(ROOT, "relay", "Dockerfile"), encoding="utf-8").read()
        self.assertIn("COPY *.py", df)
        for mod in ("app.py", "config.py", "core.py", "providers.py", "server.py"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, "relay", mod)),
                            f"缺 {mod}")

    def test_both_files_identical_apart_from_image_source(self):
        """两份 compose 只能有 build/image 两处不同，否则会悄悄漂移。

        比较时要先**剔除** image/build 行 —— build 文件多一行 build，
        逐行对齐会因为整体错位而误报。
        """
        def without_image_source(path):
            out = []
            for line in strip_comments(open(path, encoding="utf-8").read()).splitlines():
                if line.strip().startswith(("image:", "build:")):
                    continue
                out.append(line.rstrip())
            return out

        self.assertEqual(without_image_source(COMPOSE_RUN),
                         without_image_source(COMPOSE_BUILD),
                         "两份 compose 除镜像来源外必须逐行一致 —— 有人只改了其中一份")

    def test_only_one_file_declares_build(self):
        run = strip_comments(open(COMPOSE_RUN, encoding="utf-8").read())
        build = strip_comments(open(COMPOSE_BUILD, encoding="utf-8").read())
        self.assertNotIn("build:", run)
        self.assertIn("build:", build)

    def test_security_invariants(self):
        """密钥不能进容器；members.json 必须只读。"""
        for p in (COMPOSE_RUN, COMPOSE_BUILD):
            text = open(p, encoding="utf-8").read()
            self.assertIn("./config:/config:ro", text, f"{p}: /config 必须是 :ro")
            self.assertIn("./data:/data", text, f"{p}: /data 需要可写")
            self.assertIn("relay.env", text, f"{p}: 应通过 env_file 注入密钥")
            self.assertNotIn("WEBHOOK_SECRET:", text,
                             f"{p}: 密钥值不该出现在 compose 里")

    @unittest.skipUnless(HAS_YAML, "PyYAML 未安装，跳过 compose 结构校验")
    def test_yaml_is_valid_and_service_shape_matches(self):
        import yaml
        with open(COMPOSE_RUN, encoding="utf-8") as f:
            run = yaml.safe_load(f)
        with open(COMPOSE_BUILD, encoding="utf-8") as f:
            build = yaml.safe_load(f)
        self.assertEqual(list(run["services"]), list(build["services"]))
        svc = run["services"]["webhook2nats"]
        self.assertEqual(svc["volumes"], build["services"]["webhook2nats"]["volumes"])
        self.assertEqual(svc["environment"], build["services"]["webhook2nats"]["environment"])
        self.assertEqual(svc["env_file"], build["services"]["webhook2nats"]["env_file"])


class EnvExampleTest(unittest.TestCase):
    """relay.env.example 必须覆盖 server.py 真正读取的全部变量。"""

    def test_covers_every_var_server_reads(self):
        src = open(SERVER, encoding="utf-8").read()
        code_vars = set(re.findall(r'os\.environ(?:\.get)?\(\s*"(\w+)"', src))
        code_vars |= set(re.findall(r'os\.environ\[\s*"(\w+)"\s*\]', src))
        text = strip_comments(open(ENV_EXAMPLE, encoding="utf-8").read())
        documented = {l.split("=", 1)[0].strip() for l in text.splitlines() if "=" in l}

        missing = code_vars - documented
        self.assertEqual(missing, set(),
                         f"relay.env.example 未覆盖: {sorted(missing)}")

    def test_required_vars_are_uncommented(self):
        """必填项必须是生效的（未注释的）赋值，注释掉等于启动即退出。"""
        text = strip_comments(open(ENV_EXAMPLE, encoding="utf-8").read())
        active = {l.split("=", 1)[0].strip() for l in text.splitlines() if "=" in l}
        for var in ("NATS_SERVERS", "NATS_USER", "NATS_PASSWORD", "WEBHOOK_SECRET"):
            self.assertIn(var, active, f"{var} 是必填项，不能注释掉")


class GitignoreTest(unittest.TestCase):
    """密钥必须真的被忽略掉 —— 这是 .gitignore 的唯一职责。"""

    def _ignored(self, rel):
        try:
            r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT,
                               capture_output=True)
        except FileNotFoundError:
            self.skipTest("git 不可用")
        return r.returncode == 0

    def test_secrets_are_ignored(self):
        for rel in ("relay.env", "config/members.json", "config/webhook2nats.toml"):
            self.assertTrue(self._ignored(rel), f"{rel} 含真实值，必须被 .gitignore 排除")

    def test_templates_are_tracked(self):
        for rel in ("relay.env.example", "docker-compose.yml",
                    "docker-compose.build.yml",
                    "config/webhook2nats.example.toml"):
            self.assertFalse(self._ignored(rel), f"{rel} 不含环境值，应该能进库")


if __name__ == "__main__":
    unittest.main(verbosity=2)
