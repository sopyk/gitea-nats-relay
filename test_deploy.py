#!/usr/bin/env python3
"""部署产物一致性测试（标准库；PyYAML 存在时追加 compose 结构校验）。

这个项目最贵的失败模式不是代码挂掉，而是**文档、配置模板、实际代码互相漂移**：
README 说 A、示例 TOML 写 B、代码读 C，而且没有任何报错。
下面的用例把三者钉在一起。

运行：python3 test_deploy.py
"""
import ast
import glob
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

try:
    HAS_YAML = importlib.util.find_spec("yaml") is not None
except Exception:  # noqa
    HAS_YAML = False

ROOT = os.path.dirname(os.path.abspath(__file__))
TOML_EXAMPLE = os.path.join(ROOT, "config", "webhook2nats.example.toml")
ENV_EXAMPLE = os.path.join(ROOT, "relay.env.example")
# 直接 import relay/config.py，而不是起子进程：快，且失败信息能直接指到用例。
sys.path.insert(0, os.path.join(ROOT, "relay"))
import config as config_mod  # noqa: E402
# 一行真正的环境变量赋值，可带前导 '#'（模板里未启用的项就是注释掉的示例）。
_ENV_ASSIGN = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")
COMPOSE_RUN = os.path.join(ROOT, "docker-compose.yml")
COMPOSE_BUILD = os.path.join(ROOT, "docker-compose.build.yml")
README = os.path.join(ROOT, "README.md")
DOCS_DIR = os.path.join(ROOT, "docs")
SERVER = os.path.join(ROOT, "relay", "server.py")

# README「消息格式」一节承诺的统一事件类型。改了这里等于改出站契约，
# 所有已订阅的 Agent 都要跟着改 —— 必须是有意识的动作。
DOCUMENTED_KINDS = {"issue", "issue_comment", "pull_request", "review"}


def doc_files():
    """所有承载文档的文件：README + docs/*.md（按文件名排序，结果稳定）。"""
    files = [README]
    if os.path.isdir(DOCS_DIR):
        files += [os.path.join(DOCS_DIR, n) for n in sorted(os.listdir(DOCS_DIR))
                  if n.endswith(".md")]
    return files


def all_docs():
    """把全部文档拼成一份文本。

    漂移检测扫的是**内容**而不是位置：契约表写在 README 还是 docs/reference
    里，对「代码改了但文档没改」这件事没有任何区别。所以拆分文档之后，
    这些断言不该跟着搬家 —— 它们该跟着内容走。
    """
    parts = []
    for path in doc_files():
        with open(path, encoding="utf-8") as f:
            parts.append(f.read())
    return "\n\n".join(parts)


def locate(needle):
    """在文档里找到 needle，返回 (文件名, 文件内行号)；找不到返回 (None, 0)。

    报错时能指到具体文件，比只给一个「合并文本的行号」有用得多 ——
    后者在任何一个文件里都对不上。
    """
    for path in doc_files():
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if needle in line:
                    return os.path.relpath(path, ROOT), i
    return None, 0


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


def _env_vars_read_in_code():
    """扫出 relay/ 下所有非测试模块读取的环境变量。

    走 _scan_actual，这样局部别名（`s = env.get(k,"")`）也认得。
    """
    found = set()
    for path in glob.glob(os.path.join(ROOT, "relay", "*.py")):
        name = os.path.basename(path)
        if name.startswith("test_") or name in ("fixtures.py", "__init__.py"):
            continue
        with open(path, encoding="utf-8") as f:
            found |= _scan_actual(f.read())
    return found


def _discover_env_wrappers():
    """找出 relay/*.py 里「内部会读环境变量」的包装函数名。

    判据：函数体里有 os.environ / environ 的读取，且不返回任何 TOML 配置字段。
    这样 `_apply_env` 里的 `def s(k): return env.get(k,"")` 会被自动认出来，
    不必在测试里硬编码函数名 —— 改了 config.py 的写法也不用回来改测试。
    """
    names = set()
    for path in glob.glob(os.path.join(ROOT, "relay", "*.py")):
        base = os.path.basename(path)
        if base.startswith("test_") or base == "fixtures.py":
            continue
        with open(path, encoding="utf-8") as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "get"):
                    continue
                recv = call.func.value
                is_env = (isinstance(recv, ast.Attribute) and recv.attr == "environ") or \
                         (isinstance(recv, ast.Name) and recv.id in ENV_LOOKUP_ALIASES)
                if is_env and call.args and isinstance(call.args[0], ast.Name):
                    names.add(fn.name)
                    break
    return frozenset(names)


def _scan_actual(src):
    """扫出某段代码里读**环境变量**的字面量（AST，不靠正则猜）。

    正则版本踩过三次坑：只认 `env.get(` 扫不到局部别名；只认宽松模式会把
    `_apply_env` 的定义行本身算进去；跨行的 `def s(k):\n return env.get(k)`
    又匹配不上。

    AST 解决了前两个，但直接把「实参是字符串字面量的 .get()」全认下来，又会
    把 `raw.get("servers")` 这类读 TOML 的调用算进来。所以按接收者过滤：
    `os.environ.get` 无歧义；局部包装函数约定用 `k`/`key` 作形参
    （config.py 里的 `def s(k): return env.get(k, "")` 就是这种），
    而读配置字典的接收者是 `raw` / `data` 这类名字，不在白名单里。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()

    found = set()
    for node in ast.walk(tree):
        # os.environ["X"] —— 只认明确挂在 environ 上的下标，
        # 免得把 rt["broadcast_authors"] 这种读 TOML 的下标也算进来。
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            found.add(node.slice.value)
            continue

        if not (isinstance(node, ast.Call) and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            continue

        func = node.func
        # 形式一：X.get("VAR") —— os.environ.get / 局部包装函数 s(...)
        if isinstance(func, ast.Attribute) and func.attr == "get":
            recv = func.value
            if isinstance(recv, ast.Attribute) and recv.attr == "environ":
                found.add(node.args[0].value)
            elif isinstance(recv, ast.Name) and recv.id in ENV_LOOKUP_ALIASES:
                found.add(node.args[0].value)
            continue

        # 形式二：s("VAR") —— config.py 的 _apply_env 里
        #     `def s(k): return env.get(k, "")` 那层包装，调用点是裸函数名。
        #     形参是 k/env/environ 的包装函数才认，否则会把任何业务函数算进来。
        if isinstance(func, ast.Name) and func.id in ENV_WRAPPER_FUNCS:
            found.add(node.args[0].value)

    return found


# 读环境变量用的局部包装函数/别名白名单。刻意收窄 ——
# 放宽到「任意标识符.get(字符串)」会把读 TOML 字段的调用一起扫进来。
ENV_LOOKUP_ALIASES = frozenset({"k", "key", "name", "env", "environ"})

# 形参走 env/k 的环境变量包装函数（如 config.py 里的 `def s(k): return env.get(k,"")`）。
ENV_WRAPPER_FUNCS = _discover_env_wrappers()


def _reply_bodies(src):
    """server.py 里 _reply(status, "body") 的 body 文案集合。"""
    return set(re.findall(r'_reply\(\d+,\s*"([\w-]+)"', src))


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


def extract_doc_toml_block(anchor):
    """取所有文档里，紧跟 anchor 之后出现的第一个 ```toml 代码块。

    跨文件找，而不是只看 README：内容搬到 docs/ 之后，这个函数不该失效。
    找不到返回 None —— 之前用 text.index 抛异常，报的是「substring not found」，
    完全看不出是「文档里没有这个示例」。
    """
    for path in doc_files():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if anchor not in text:
            continue
        i = text.index(anchor)
        try:
            start = text.index("```toml", i)
        except ValueError:
            continue
        start = text.index("\n", start) + 1
        end = text.index("```", start)
        return text[start:end]
    return None


# 旧名保留：外部若有引用不至于断，但内部一律走 extract_doc_toml_block。
extract_readme_toml_block = extract_doc_toml_block


def _contract_section():
    """HTTP 契约那一节的正文（跨文件找）。

    原来只截 README 里 `## HTTP 契约` 到 `### 去重与重试语义` 之间。
    契约表搬到 docs/reference.md 之后该跟着走 —— 它盯的是「代码改了、
    契约表没改」，跟这段文字写在哪个文件里毫无关系。
    """
    start_markers = ("HTTP 契约",)
    end_markers = ("去重与重试语义", "@提及解析")
    for path in doc_files():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        i = next((text.index(m) for m in start_markers if m in text), None)
        if i is None:
            continue
        ends = [text.index(m) for m in end_markers if m in text and
                text.index(m) > i]
        end = min(ends) if ends else len(text)
        return text[i:end]
    raise AssertionError("所有文档里都找不到 HTTP 契约那一节")


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


class TemplateUsabilityTest(unittest.TestCase):
    """模板必须**真的能用**，而不是「在库里就算数」。

    背景是一次真实的误解要避免：为了不泄露部署环境里的用户名/IP，
    把真实值全换成了占位符 —— 这件事本身是对的。但它有个反向风险：
    **万一连模板本身也被误清理了，仓库就不再是一个能用的起点**。
    「不含环境值」和「不可用」是两件事，这里盯的是后者。

    「不含环境值」由 relay/test_internals.py::RoutingTest 的隐私守卫负责；
    本类只保证模板齐全、且照 README 的步骤走能配出一个能加载的配置。
    """

    def setUp(self):
        with open(TOML_EXAMPLE, "rb") as f:
            self.toml = tomllib.load(f)

    def test_both_templates_are_tracked(self):
        """模板进不了库 = 别人下下来没法用。这是硬要求。"""
        try:
            r = subprocess.run(["git", "ls-files"], cwd=ROOT,
                               capture_output=True, text=True, timeout=30)
            tracked = set(r.stdout.split())
        except (subprocess.SubprocessError, OSError):
            self.skipTest("拿不到 git ls-files")
        for name in ("config/webhook2nats.example.toml", "relay.env.example"):
            self.assertIn(name, tracked,
                          f"{name} 没进库 —— 别人克隆下来就没法部署")

    def test_toml_template_contains_every_required_section(self):
        for sec in ("nats", "providers", "routing", "dedup", "logging"):
            self.assertIn(sec, self.toml, f"示例配置缺 [{sec}] 段")
        # 必填项必须在模板里出现且是占位符，而不是被删掉「反正有默认值」
        self.assertIn("servers", self.toml["nats"])
        self.assertIn("user", self.toml["nats"])
        self.assertIn("password", self.toml["nats"])
        for provider in self.toml.get("providers", {}).values():
            self.assertIn("signature", provider,
                          "provider 缺 signature 段 —— 没有它无法验签")
            self.assertIn("secret", provider["signature"])

    def test_env_template_covers_the_required_vars(self):
        """必填环境变量必须在 relay.env.example 里，且是**未注释**的。

        未注释很关键：模板里以 `#` 开头的是「可选项示例」，用户会以为
        不填也能跑。必填项被注释掉的话，照抄的人会漏填，启动时才报错。
        """
        with open(ENV_EXAMPLE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        active = {}
        for line in lines:
            if line.startswith("#") or "=" not in line:
                continue
            active[line.split("=", 1)[0].strip()] = line.split("=", 1)[1]
        for var in ("NATS_SERVERS", "NATS_USER", "NATS_PASSWORD",
                    "WEBHOOK_SECRET"):
            self.assertIn(
                var, active,
                f"{var} 是必填的，却没在 relay.env.example 里以生效状态出现 "
                f"（注释掉的话用户会漏填）")

    def test_a_fresh_clone_can_produce_a_loadable_config(self):
        """照 README 的步骤拼一份配置出来，必须能通过配置加载与校验。

        这是「下下去能用」的可执行版本：拼不出来就说明模板缺东西或文档
        与模板对不上。允许纯环境变量模式（README 说了不配 TOML 也能跑），
        所以这里补的是**最小必需项**，不是整份模板。
        """
        import tempfile
        env = {"NATS_SERVERS": "nats://x:4222", "NATS_USER": "u",
               "NATS_PASSWORD": "p", "WEBHOOK_SECRET": "s"}
        tmp = tempfile.mkdtemp()
        # 1) 不放任何 TOML —— README 承诺的「纯环境变量模式」必须能起
        cfg = config_mod.load(path=os.path.join(tmp, "absent.toml"), env=env)
        self.assertTrue(cfg.enabled_providers(),
                        "纯环境变量模式下没有可用 provider")

        # 2) 原样用示例 TOML + 环境变量 —— 模板本身必须自洽可用
        cfg2 = config_mod.load(path=TOML_EXAMPLE, env=env)
        self.assertTrue(cfg2.enabled_providers(),
                        "示例 TOML 原样加载后没有可用 provider")

    def test_readme_quickstart_matches_the_templates(self):
        """README 的快速上手必须提到两个模板文件，否则用户不知道去哪儿。"""
        with open(README, encoding="utf-8") as f:
            readme = f.read()
        for token in ("relay.env.example", "members.json"):
            self.assertIn(token, readme,
                          f"README 的快速上手没提到 {token}，新用户会不知道要准备它")


class DockerHubPublishingTest(unittest.TestCase):
    """Docker Hub 推送策略的护栏。

    策略：**只在稳定版 git tag 推送时推 Docker Hub**，且 tag 集合与 GHCR 一致、
    「预发布不许动 latest」两边都生效、双架构两边都验。

    这些断言全是在本地模拟时抓出真 bug 之后补的 —— 不模拟只读代码的话，
    里面两个错（预发布判定漏 `-rc1`、「禁 latest」没排除稳定版）会把每一次
    正式版发布都判成失败，而光看代码完全看不出来。
    """

    WORKFLOW = os.path.join(ROOT, ".github", "workflows", "publish.yml")

    def _wf(self):
        with open(self.WORKFLOW, encoding="utf-8") as f:
            return f.read()

    def test_dockerhub_is_targeted_only_by_stable_releases(self):
        """Docker Hub 只在稳定版 git tag 推送时用。

        同时保三件事：那边第一个镜像干净、不堆分支 tag、
        分支构建（可能来自他人 PR）不需要 DockerHub 凭据。
        """
        wf = self._wf()
        self.assertIn("push/refs/tags/v*", wf,
                      "决策脚本里找不到稳定版判断分支")
        self.assertIn("只推 GHCR", wf, "决策脚本缺少非正式版的说明分支")
        for step in re.findall(r"- name: Log in to Docker Hub(.*?)\n\n", wf, re.S):
            self.assertIn("dockerhub == 'true'", step,
                         "Docker Hub 登录没挂条件 —— 分支构建也会去要凭据")

    def test_dockerhub_credentials_are_validated_up_front(self):
        """secret 缺失必须明确报错，而不是一个含糊的 unauthorized。"""
        wf = self._wf()
        self.assertIn("DOCKERHUB_USERNAME", wf)
        self.assertIn("DOCKERHUB_TOKEN", wf)
        self.assertIn("缺少 secret", wf,
                      "没有 secret 缺失的显式校验 —— 名字填错只会得到含糊报错")

    def test_tag_policy_covers_every_registry(self):
        wf = self._wf()
        self.assertIn(
            'is_prerelease = any(f"-{p}" in ref', wf,
            "预发布判定写错了 —— 漏掉 -rc1 这种最常见形式")
        self.assertIn("if is_prerelease:", wf)
        self.assertIn("预发布版本不允许移动 latest", wf,
                      "「不许带 latest」没限定在预发布时，会把正式版发布判失败")
        self.assertIn("缺 latest", wf, "缺少稳定版必须带 latest 的断言")
        self.assertIn("tag 集合与", wf,
                      "没有两个 registry tag 集合一致性的断言")

    def test_architecture_verification_covers_every_registry(self):
        """双架构必须逐个 registry 验。

        本地是 arm64，验证不了 amd64 有没有漏推，只能读远端 manifest。
        只验 GHCR 的话，Docker Hub 上出了 arm64-only 镜像也没人发现 ——
        而那正是「别人 pull 下来在自己机器上跑不了」的成因。
        """
        wf = self._wf()
        verify = wf[wf.index("Verify both architectures"):]
        # 必须覆盖 Docker Hub 的路径，而不是只查 tags[0]
        self.assertIn("docker.io", verify, "双架构验证只覆盖了 GHCR")
        # 实现从手写 curl 换成 buildx imagetools 后，这里从「必须出现
        # auth.docker.io/token」改成「必须用 imagetools」—— 新版由 buildx
        # 统一处理两个 registry 的鉴权。等价强度保留，关键在下一条的循环。
        self.assertIn("imagetools inspect", verify,
                      "没有用 imagetools —— 换回手写 curl 的话，要为两个 "
                      "registry 分别处理各自不同的鉴权端点")
        self.assertIn("while IFS= read -r path", verify,
                      "没有循环遍历 registry —— 只会验第一个，Docker Hub 被漏掉")
        self.assertIn("rpartition(':')", verify,
                      "没从镜像引用里剥出 registry 路径")
        self.assertIn("缺少架构", verify, "没有缺架构的判断")

    def test_dockerhub_policy_is_documented(self):
        with open(os.path.join(ROOT, "docs", "operations.md"),
                  encoding="utf-8") as f:
            ops = f.read()
        self.assertIn("仅正式版", ops,
                      "运维手册没写 Docker Hub 只推正式版，"
                      "用户会疑惑分支构建为什么没同步")


class VersionConsistencyTest(unittest.TestCase):
    """版本号只有一处真源，其余地方不许再硬编码。

    「版本号出现在多个地方、有的更新了有的没更新」是发版事故里最常见也最难
    查的一种：各处看起来都像对的，只有对照着比才能发现有一个是旧的。
    所以这里把「比一遍」变成每次跑测试都做的事。
    """

    def setUp(self):
        self.version = Path(ROOT, "VERSION").read_text(encoding="utf-8").strip()
        self.assertTrue(self.version, "VERSION 文件是空的")
        self.assertRegex(
            self.version, r"^\d+\.\d+\.\d+$",
            f"VERSION 不是规范的三段式 semver: {self.version!r}")

    def test_changelog_top_entry_matches_version(self):
        path = Path(ROOT, "CHANGELOG.md")
        self.assertTrue(path.exists(), "没有 CHANGELOG.md")
        head = path.read_text(encoding="utf-8").split("---", 1)[-1]
        found = re.findall(r"^##\s+(\d+\.\d+\.\d+)", head, re.M)
        self.assertTrue(found, "CHANGELOG 里找不到版本标题")
        self.assertEqual(
            found[0], self.version,
            f"CHANGELOG 最新版本是 {found[0]}，VERSION 文件是 {self.version} —— "
            f"发版时忘了改其中一个")

    def test_no_stale_version_literals_anywhere(self):
        """除 VERSION / CHANGELOG 外，不该再出现别的版本号字面量。

        扫的是「形如 x.y.z 且带 pre-release 后缀」这种**只在版本语境下**
        才出现的写法（`-rc1`、`-beta.2`、`-alpha`）。光扫 `1.0.0` 会误伤
        端口号、镜像 label 之类。
        """
        offenders = []
        for path in doc_files() + [os.path.join(ROOT, "docker-compose.yml"),
                                    os.path.join(ROOT, "docker-compose.build.yml"),
                                    os.path.join(ROOT, "relay", "Dockerfile")]:
            rel = os.path.relpath(path, ROOT)
            if rel in ("CHANGELOG.md", "VERSION"):
                continue
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    for m in re.finditer(r"\d+\.\d+\.\d+-(?:rc|beta|alpha)", line):
                        offenders.append(f"{rel}:{i}: {m.group(0)}")
        self.assertEqual(
            offenders, [],
            f"这些地方出现了预发布版本号字面量（应只由 CI 从 VERSION 推导）: {offenders}")

    def test_compose_does_not_pin_a_version(self):
        """compose 不许写死版本号。

        写死了就要人记得更新 —— 而这正是这套护栏要消灭的那类遗漏。
        compose 只用 `:latest`，版本身份由 CI 产出的 tag 决定。

        只看**生效的配置行**：注释里可以（也应该）写「想钉版本就改成
        `:1.0`」这类说明，那是给人看的，不是 compose 实际读的值。
        """
        for name in ("docker-compose.yml", "docker-compose.build.yml"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if line.lstrip().startswith("#"):
                        continue
                    if re.search(r"image:.*:\d+\.\d+", line):
                        self.fail(
                            f"{name}:{i} 写死了镜像版本号: {line.strip()} —— "
                            f"改用 :latest，版本身份交给 CI")

    def test_ci_reads_the_version_file(self):
        """CI 必须从 VERSION 文件读，而不是在 workflow 里再写一个版本号。"""
        with open(os.path.join(ROOT, ".github", "workflows", "publish.yml"),
                  encoding="utf-8") as f:
            wf = f.read()
        self.assertIn("cat VERSION", wf,
                      "CI 没有从 VERSION 文件读版本号")
        self.assertIn("VERSION=${{ steps.ver.outputs.version }}", wf,
                      "CI 没把版本号作为 build-arg 传进镜像")

    def test_dockerfile_takes_version_from_build_arg(self):
        """镜像里的版本号只能来自构建期注入，不能烤死。"""
        with open(os.path.join(ROOT, "relay", "Dockerfile"),
                  encoding="utf-8") as f:
            df = f.read()
        self.assertIn('ARG VERSION=""', df, "Dockerfile 没有 ARG VERSION")
        self.assertNotRegex(
            df, r"WEBHOOK2NATS_VERSION=\d",
            "Dockerfile 把版本号烤死了 —— 每次发版都得改它")

    def test_workflow_enforces_latest_only_for_stable(self):
        """「latest 只跟正式版走」必须有断言强制，不能只写在文档里。

        这条正是为了兑现「别人跟着 latest 拉，不会拿到测试版」：
        预发布构建若产出 latest，CI 必须当场失败。
        """
        with open(os.path.join(ROOT, ".github", "workflows", "publish.yml"),
                  encoding="utf-8") as f:
            wf = f.read()
        self.assertIn("Assert tag policy", wf, "CI 里没有 tag 策略断言")
        self.assertIn("预发布版本不允许移动 latest", wf,
                      "tag 策略断言里没有预发布的检查")
        self.assertIn("缺 latest", wf, "tag 策略断言里没有稳定版的检查")
        # 预发布判定必须覆盖 -rc1 这种「数字紧跟」的形式
        self.assertIn('f"-{p}" in ref', wf,
                      "预发布判定写错了 —— 漏掉 -rc1 这种最常见形式")

    def test_tag_policy_assertion_parses_image_refs_correctly(self):
        """反向自检：断言必须真的能认出 latest，且能区分 registry。

        metadata-action 的 json 输出里，tags 是**完整镜像引用**
        （`ghcr.io/sopyk/webhook2nats:latest`），不是裸 tag 名。直接拿去
        和 'latest' 比较永远不相等 —— 那条断言会每次都判「缺 latest」，
        把每一次正式发布都卡死。这个坑踩过一次，所以钉死。

        推两个 registry 时每个逻辑 tag 出现两次，所以正确写法是按
        「registry 路径 → tag 集合」分组，而不是拍平成一张大表。
        """
        with open(os.path.join(ROOT, ".github", "workflows", "publish.yml"),
                  encoding="utf-8") as f:
            wf = f.read()
        self.assertIn('rpartition(":")', wf,
                      "断言没有从镜像引用里剥出路径和 tag，"
                      "会把每次发布都判成缺 latest")
        self.assertIn("by_reg", wf,
                      "断言没有按 registry 分组 —— 推两个 registry 时"
                      "逻辑 tag 会重复出现，拍平就分不清谁是谁")


class GitignoreCoverageTest(unittest.TestCase):
    """.gitignore 必须真的挡住本地文件和过程文件。

    （取代了原来那条只查 3 个文件名、2 个用例的 GitignoreTest ——
     它既没覆盖过程文件，也没覆盖 docker-compose.override 这类本地覆盖文件。
     保留两个同名类是隐患：Python 里后定义的会静默遮蔽先定义的，
     跑出来的用例名完全看不出哪个在生效。）

    为什么需要测试来盯一份纯文本：它是**唯一一道不靠自觉的防线**。
    `git add .` 不会问你「这个文件带不带环境值」，而 `.gitignore` 写漏一条
    规则，等到东西进了公开仓库才发现就晚了 —— 真实配置一旦 push，
    即使后续 commit 删掉，git 历史里还留着，必须改写历史才能真正移除。

    下面这份列表是**实测出来的**：拿原规则去 check-ignore 一批真实文件名，
    `docker-compose.override.yml`（compose 会自动合并它，正是大家放本地
    密钥的地方）、根目录的 `members.json`、`*.log`、`*.env.local` 全部漏网。
    """

    # 必须被忽略。新增规则就往对应组里加。
    MUST_IGNORE = [
        # 真实密钥与配置
        "relay.env", "relay.env.local", "prod.env", ".env", ".env.local",
        "config/webhook2nats.toml", "config/members.json",
        "members.json",                       # 不在 config/ 下也要挡
        "nested/deep/webhook2nats.toml",
        # 任意深度的也要挡。少了这两条的话，只删掉 `**/...` 那两行规则
        # 而根目录那条还在，上面的断言依然全绿 —— 规则失效了测不出来。
        "nested/deep/members.json",
        "nested/deep/relay.env",
        # 证书与凭据
        "server.pem", "tls.key", "ca.crt", "bundle.p12",
        # 运行产物
        "data/seen.json", "data/logs/relay.log",
        "relay.log", "relay.pid", "seen.json",
        # 本地 compose 覆盖（compose 会自动合并它）
        "docker-compose.override.yml",
        "docker-compose.override.yaml",
        # 编辑器与工具产物
        ".DS_Store", "__pycache__/core.cpython-312.pyc", "app.pyc",
        "main.py.swp", "README.md~", "config.toml.bak", "config.toml.orig",
        ".idea/workspace.xml", ".vscode/settings.json",
        # Python 构建与工具缓存
        ".coverage", "htmlcov/index.html", "webhook2nats.egg-info/PKG-INFO",
        ".venv/bin/python", "build/lib/core.py", "dist/core.py",
        ".pytest_cache/CACHEDIR.TAG", ".mypy_cache/3.12/stub.py",
        # 调试残留
        "nohup.out", "core.12345",
    ]

    # 必须**不能**被忽略 —— 模板与源码要能进库。
    MUST_TRACK = [
        "relay.env.example",
        "config/webhook2nats.example.toml",
        "README.md", "docker-compose.yml", "docker-compose.build.yml",
        "test_deploy.py", "relay/logs.py", "relay/server.py",
        "docs/reference.md", "docs/operations.md",
    ]

    def _check_ignore(self, path):
        r = subprocess.run(["git", "check-ignore", "--no-index", "-q", path],
                           cwd=ROOT, capture_output=True)
        if r.returncode not in (0, 1):
            self.skipTest(f"git check-ignore 跑不起来: {r.stderr[:80]!r}")
        return r.returncode == 0

    def test_local_and_temp_files_are_ignored(self):
        leaked = [p for p in self.MUST_IGNORE if not self._check_ignore(p)]
        self.assertEqual(
            leaked, [],
            f"这些本地/临时文件没被 .gitignore 挡住，git add . 会带进仓库: {leaked}")

    def test_templates_and_source_are_not_ignored(self):
        blocked = [p for p in self.MUST_TRACK if self._check_ignore(p)]
        self.assertEqual(
            blocked, [], f"这些该进库的文件被 .gitignore 挡住了: {blocked}")

    def test_no_rule_is_redundantly_covering_the_probe(self):
        """反向自检：断言必须真的依赖它声称的那条规则。

        gitignore 经常「靠巧合挡住」——某条规则被另一条顺带覆盖了。
        那样规则删掉了断言还是绿的，改动就不再受保护。这里在干净仓库里
        只放**去掉该规则**的 gitignore，验证目标文件就漏网了。

        规则片段要选得准：gitignore 里**不带斜杠的模式在任意层级都匹配**，
        所以 `members.json` 一条就够，写 `**/members.json` 是多余的 ——
        而那条多余的规则会让这条反向自检**永远抓不到真实回归**
        （删掉真规则，多余那条还在，断言照样绿）。这个坑踩过。
        """
        import tempfile as _tf
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            original = f.read()
        # (要删掉的**整行规则**, 用它验证的探针路径)
        #
        # 探针路径必须选**只有这条规则能挡**的位置。例如
        # `config/webhook2nats.toml` 就算删掉 `webhook2nats.toml` 规则，
        # 仍然会被 `config/*` 顺手挡住 —— 拿它当探针，断言就永远发现不了
        # 那条规则真的没了。
        for rule_line, probe_file in (
            ("docker-compose.override.yml", "docker-compose.override.yml"),
            ("members.json", "nested/deep/members.json"),
            ("webhook2nats.toml", "nested/webhook2nats.toml"),
            ("*.log", "deep/dir/relay.log"),
            ("*.pem", "certs/server.pem"),
        ):
            with self.subTest(probe=probe_file):
                lines = original.splitlines()
                self.assertIn(
                    rule_line, [l.strip() for l in lines],
                    f"没找到规则 {rule_line!r}（整行比对），反向自检的前提没了")
                stripped = "\n".join(l for l in lines if l.strip() != rule_line)
                probe = _tf.mkdtemp()
                try:
                    with open(os.path.join(probe, ".gitignore"), "w",
                              encoding="utf-8") as f:
                        f.write(stripped)
                    subprocess.run(["git", "init", "-q"], cwd=probe, check=True)
                    r = subprocess.run(
                        ["git", "check-ignore", "--no-index", "-q", probe_file],
                        cwd=probe, capture_output=True)
                    self.assertEqual(
                        r.returncode, 1,
                        f"删掉规则 {rule_line!r} 后 {probe_file} 仍被忽略 —— "
                        f"有别的规则在兜底，这条断言抓不到真实回归")
                finally:
                    subprocess.run(["rm", "-rf", probe], check=False)

    def test_nothing_sensitive_is_already_tracked(self):
        """已经进了库的东西现在就不在里面。

        这是唯一能回答「历史上有没有漏过」的检查 —— gitignore 只管未来。
        """
        try:
            r = subprocess.run(["git", "ls-files"], cwd=ROOT,
                               capture_output=True, text=True, timeout=30)
            tracked = r.stdout.split()
        except (subprocess.SubprocessError, OSError):
            self.skipTest("拿不到 git ls-files")
        self.assertTrue(tracked, "一个跟踪文件都没有，扫描逻辑坏了")
        for name in tracked:
            base = os.path.basename(name)
            self.assertFalse(
                name.endswith((".log", ".pem", ".key", ".pyc", ".bak")),
                f"不该被跟踪的文件已在库中: {name}")
            self.assertNotIn(
                base, ("members.json", "webhook2nats.toml", "relay.env"),
                f"真实配置/密钥已被提交（一旦 push 进历史就等于泄露，"
                f"必须改写历史才能真正移除）: {name}")


class ReadmeConsistencyTest(unittest.TestCase):
    """文档与实际产物的一致性（扫 README + docs/*.md）。"""

    def test_relative_links_resolve(self):
        """README 和每一份 docs 里的相对链接都必须指向真实存在的文件。

        docs/*.md 深了一层目录，链接要带 ../。拼错了不会渲染成死链而已 ——
        GitHub 上照着文档操作的人会直接踩空，所以这条必须盯住。
        """
        broken = []
        checked = 0
        for path in doc_files():
            base = os.path.dirname(path)          # 链接是相对**所在文件**解析的
            with open(path, encoding="utf-8") as f:
                text = f.read()
            targets = re.findall(r"\]\((?!https?:)([^)#]+)\)", text)
            checked += len(targets)
            for t in targets:
                if not os.path.exists(os.path.normpath(os.path.join(base, t))):
                    rel = os.path.relpath(path, ROOT)
                    broken.append(f"{rel} → {t}")
        self.assertTrue(checked, "所有文档里一个相对链接都没有，可疑")
        self.assertEqual(broken, [], f"文档引用了不存在的文件: {broken}")

    def test_readme_stays_a_readme(self):
        """README 得是 README —— 长了没人读，等于没写。

        不是行数洁癖：技术细节一旦堆回 README，它就退化成一份没人翻的
        大文件，而 docs/ 里的内容会因为「README 里也有一份」而两边漂移。
        上限留了余量，但仍足以挡住「把 docs 又粘回来了」。
        """
        with open(README, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertLess(len(lines), 260,
                        f"README 已经 {len(lines)} 行，太长了；"
                        f"技术细节该进 docs/，README 只留功能与上手")

    def test_every_doc_is_linked_from_the_readme(self):
        """docs/ 里每份文档都要能从 README 点进去。

        否则它就成了「存在但没人找得到」的文件 —— 等于白写。
        """
        with open(README, encoding="utf-8") as f:
            text = f.read()
        for path in doc_files():
            rel = os.path.relpath(path, ROOT)
            if rel == "README.md":
                continue
            self.assertIn(f"({rel})", text,
                          f"{rel} 没有被 README 引用，会没人找得到")

    def test_provider_block_matches_example_config(self):
        """文档里的 provider 示例必须和 config/webhook2nats.example.toml 一致。

        这是最容易漂移的一处：改了模板忘了同步文档，读者就会照着
        一个不存在的配置去部署。
        """
        block = extract_doc_toml_block("通用模型")
        self.assertIsNotNone(block, "文档里找不到 provider 示例代码块")
        readme_secs = parse_toml_sections(block)
        with open(TOML_EXAMPLE, encoding="utf-8") as f:
            file_secs = parse_toml_sections(f.read())

        for section in ("providers.gitea.headers",
                        "providers.gitea.signature",
                        "providers.gitea.events",
                        "providers.gitea.event_types"):
            self.assertIn(section, readme_secs, f"文档示例缺 {section}")
            self.assertIn(section, file_secs, f"示例配置缺 {section}")
            self.assertEqual(
                readme_secs[section], file_secs[section],
                f"section {section} 在文档和示例配置之间不一致")

    def test_documented_kind_set_matches_example(self):
        text = all_docs()
        with open(TOML_EXAMPLE, "rb") as f:
            cfg = tomllib.load(f)
        used = set(cfg["providers"]["gitea"]["events"].values())
        used |= set(cfg["providers"]["gitea"]["event_types"].values())
        # 文档必须把用到的每个 kind 都写进那张表
        for kind in used:
            self.assertIn(f"`{kind}`", text, f"文档未文档化 kind {kind!r}")

    def test_both_compose_files_are_documented(self):
        """两份 compose 都得被文档提到（README 或 docs 任一处都行）。"""
        docs = all_docs()
        for name in ("docker-compose.yml", "docker-compose.build.yml"):
            self.assertIn(name, docs, f"文档里没提到 {name}")


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
        with open(df_path, encoding="utf-8") as f:
            df = f.read()
        # 模块化之后是多个 .py，COPY 必须逐个列出或用 *.py，
        # 否则镜像里少了模块，启动时 import 就炸。
        self.assertTrue("COPY *.py /app/" in df or "COPY app.py" in df,
                        "Dockerfile 的 COPY 没覆盖全部模块")
        self.assertIn("app.py", df.split("CMD")[-1], "容器入口应指向 app.py")

    def test_every_module_is_copied(self):
        """镜像要能 import 到每一个产品模块，别漏了新增的。"""
        with open(os.path.join(ROOT, "relay", "Dockerfile"), encoding="utf-8") as f:
            df = f.read()
        copy_line = next((l for l in df.splitlines() if l.startswith("COPY")), "")
        # 最后一个 token 是目标路径（/app/），其余都是源文件
        modules = set(copy_line.split()[1:-1])
        for mod in ("app.py", "config.py", "core.py", "providers.py", "server.py"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, "relay", mod)),
                            f"缺 {mod}")
            self.assertIn(mod, modules, f"{mod} 没被 COPY 进镜像")

    def test_tests_are_not_baked_into_image(self):
        """测试文件不进交付镜像。

        里面有你测试用的密钥占位符（s3cr3t）和内网主机名，
        不该出现在别人拉下来的镜像里。测试在源码树里跑就行。
        """
        with open(os.path.join(ROOT, "relay", "Dockerfile"), encoding="utf-8") as f:
            df = f.read()
        copy_line = next((l for l in df.splitlines() if l.startswith("COPY")), "")
        self.assertNotIn("*.py", copy_line,
                         "COPY *.py 会把测试文件一起打进镜像")
        for t in ("test_relay.py", "test_internals.py", "fixtures.py"):
            self.assertNotIn(t, df, f"{t} 不应出现在 Dockerfile 里")

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

    def test_compose_does_not_shadow_config_file(self):
        """compose 不得设置路径类环境变量。

        环境变量优先级高于配置文件。在 compose 里写死 MEMBERS_FILE / DEDUP_FILE
        （哪怕值和默认值一样），会让运维在 webhook2nats.toml 里的对应设置**静默失效**，
        且没有任何提示。Dockerfile 里烤 ENV 同理。
        """
        for p in (COMPOSE_RUN, COMPOSE_BUILD):
            body = strip_comments(open(p, encoding="utf-8").read())
            for var in ("MEMBERS_FILE", "DEDUP_FILE", "LISTEN", "CONFIG_FILE"):
                self.assertNotIn(f"{var}:", body,
                                 f"{os.path.basename(p)} 里的 {var} 会盖掉配置文件")
        df = open(os.path.join(ROOT, "relay", "Dockerfile"), encoding="utf-8").read()
        for line in df.splitlines():
            if line.startswith("ENV "):
                for var in ("MEMBERS_FILE", "DEDUP_FILE", "LISTEN", "CONFIG_FILE"):
                    self.assertNotIn(f"{var}=", line,
                                     f"Dockerfile 里的 {var} 会盖掉配置文件")

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
        """relay.env.example 必须覆盖代码真正读取的全部变量。

        注意扫的是 relay/ 下全部非测试模块 —— 重构把 os.environ 全搬进了
        config.py，这一条护栏曾经因为只扫 server.py 而永远通过。
        """
        code_vars = _env_vars_read_in_code()
        self.assertTrue(code_vars, "没扫到任何环境变量读取，扫描逻辑坏了")
        # 注意不能 strip_comments：relay.env.example 里的可选项是**注释掉的示例**
        # （`# RELAY_ID=...`），那正是它们的文档化形式；剥掉就等于要求全部启用。
        # 这里只要文件里出现过 "VAR=" 就算已记录。
        with open(ENV_EXAMPLE, encoding="utf-8") as f:
            raw = f.read()
        # 只认真正的 VAR= 形状。早先用 `if "=" in l` 把散文中出现的等号也
        # 当成赋值，于是「允许走全厅公告的作者登录名，逗号分隔。留空 = 不发
        # 任何公告。」这类说明句会变成一个"变量名"，只能靠硬编码例外把它从
        # 结果里减掉。正则要求变量名是大写环境变量形状，例外就不需要了。
        documented = {m.group(1) for m in
                      (_ENV_ASSIGN.match(line) for line in raw.splitlines())
                      if m}
        # 反向自检：正则没把真变量也滤掉，否则这条测试就成了空转的断言。
        self.assertIn("WEBHOOK_SECRET", documented,
                      "解析器没认出已存在的必填变量，ENV_ASSIGN 写错了？")
        self.assertIn("CONFIG_FILE", documented)

        # 这几个由 compose 的 environment: 提供，值是容器内路径、没有环境相关性，
        # 放进 env 文件反而会让运维以为可以在那里改成宿主路径。
        # WEBHOOK2NATS_VERSION 同理 —— 它由 Dockerfile 的 ARG 在构建期烤进
        # 镜像，写进 relay.env 只会让人以为改了它就能换版本。
        # WEBHOOK2NATS_VERSION / WEBHOOK2NATS_COMMIT 同理 —— 它们由 Dockerfile 的
        # ARG 在**构建期**烤进镜像（CI 从仓库根的 VERSION 文件传入），
        # 写进 relay.env 只会让人以为改了它就能查出版本，实际上改的是
        # 运行时环境变量，与镜像里已经烤好的那个无关。
        provided_elsewhere = {"MEMBERS_FILE", "DEDUP_FILE", "LISTEN",
                              "WEBHOOK2NATS_VERSION", "WEBHOOK2NATS_COMMIT"}

        missing = code_vars - documented - provided_elsewhere
        self.assertEqual(missing, set(),
                         f"relay.env.example 未覆盖: {sorted(missing)}")

        # 反过来：凡在 env 文件里出现的，必须真的是代码读到的变量，
        # 否则模板里会攒下已经废弃的变量名。
        stale = documented - code_vars
        self.assertEqual(stale, set(),
                         f"relay.env.example 里有代码不再读取的变量: {sorted(stale)}")

    def test_scan_actually_finds_the_env_reads(self):
        """反向自检：确保上面两条不是又一个空转的断言。

        写这条是因为前两版扫描分别用了 `env.get(` 和 `os.environ.get(`，而代码里
        是 `_apply_env(cfg, env)` + `env.get(` —— 两次都一条没扫到，断言永远是假的。
        """
        src = open(os.path.join(ROOT, "relay", "config.py"), encoding="utf-8").read()
        found = _scan_actual(src)
        self.assertIn("WEBHOOK_SECRET", found, "没扫到 WEBHOOK_SECRET，模式写错了？")
        self.assertIn("NATS_SERVERS", found, "没扫到 NATS_SERVERS，模式写错了？")
        self.assertIn("CONFIG_FILE", found)
        self.assertGreaterEqual(len(found), 10, f"只扫到 {len(found)} 个: {found}")

    def test_config_file_var_is_documented(self):
        """CONFIG_FILE 这个变量，文档与 relay.env.example 都该提到。"""
        docs = all_docs()
        env_ex = open(ENV_EXAMPLE, encoding="utf-8").read()
        self.assertIn("CONFIG_FILE", docs, "文档未记录 CONFIG_FILE")
        self.assertIn("CONFIG_FILE", env_ex, "relay.env.example 未提及 CONFIG_FILE")

    def test_http_status_codes_match_implementation(self):
        """HTTP 契约表必须覆盖 server.py 实际返回的每一个状态码。

        手写状态码列表必然漂移 —— 之前就漏了 5 个（404 / 400 bad-length /
        413 / 400 missing-delivery / 200 ignored），而没有任何东西会发现。
        """
        with open(os.path.join(ROOT, "relay", "server.py"), encoding="utf-8") as f:
            src = f.read()
        implemented = {int(c) for c, _ in re.findall(r'_reply\((\d+),\s*"([\w-]+)"', src)}

        contract = _contract_section()
        documented = {int(c) for c in re.findall(r'`(\d{3})', contract)}

        self.assertTrue(documented, "契约表里一个状态码都没解析到")
        missing = implemented - documented
        self.assertEqual(missing, set(),
                         f"server.py 返回了但契约表没写: {sorted(missing)}")

    def test_every_reply_has_a_documented_body(self):
        """契约表里的每个 body 文案也要对得上，否则平台侧排查会摸不着头脑。"""
        with open(os.path.join(ROOT, "relay", "server.py"), encoding="utf-8") as f:
            src = f.read()
        bodies = _reply_bodies(src)
        self.assertIn("accepted", bodies, "没抓到响应体，扫描逻辑坏了")

        contract = _contract_section()
        for body in bodies:
            self.assertIn(f"`{body}`", contract, f"响应体 {body!r} 未写进契约表")

    def test_no_stale_toml_is_coming_claims(self):
        """README 宣称 TOML 已落地，散落各处的「尚未落地」就是自相矛盾。"""
        readme = open(README, encoding="utf-8").read()
        for stale in ("尚未落地", "TOML 落地后", "当前代码仍走环境变量",
                      "代码尚未落地"):
            self.assertNotIn(stale, readme,
                             f"README 仍写着「{stale}」，但实现状态表标了已可用")
        env_ex = open(ENV_EXAMPLE, encoding="utf-8").read()
        self.assertNotIn("尚未落地", env_ex)

    def test_required_vars_are_uncommented(self):
        """必填项必须是生效的（未注释的）赋值，注释掉等于启动即退出。"""
        text = strip_comments(open(ENV_EXAMPLE, encoding="utf-8").read())
        active = {l.split("=", 1)[0].strip() for l in text.splitlines() if "=" in l}
        for var in ("NATS_SERVERS", "NATS_USER", "NATS_PASSWORD", "WEBHOOK_SECRET"):
            self.assertIn(var, active, f"{var} 是必填项，不能注释掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
