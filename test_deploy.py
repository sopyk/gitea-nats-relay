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

try:
    HAS_YAML = importlib.util.find_spec("yaml") is not None
except Exception:  # noqa
    HAS_YAML = False

ROOT = os.path.dirname(os.path.abspath(__file__))
TOML_EXAMPLE = os.path.join(ROOT, "config", "webhook2nats.example.toml")
ENV_EXAMPLE = os.path.join(ROOT, "relay.env.example")
# 一行真正的环境变量赋值，可带前导 '#'（模板里未启用的项就是注释掉的示例）。
_ENV_ASSIGN = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")
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
        provided_elsewhere = {"MEMBERS_FILE", "DEDUP_FILE", "LISTEN",
                              "WEBHOOK2NATS_VERSION"}

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
        """CONFIG_FILE 是新增的变量，README 与 relay.env.example 都该提到。"""
        readme = open(README, encoding="utf-8").read()
        env_ex = open(ENV_EXAMPLE, encoding="utf-8").read()
        self.assertIn("CONFIG_FILE", readme, "README 未文档化 CONFIG_FILE")
        self.assertIn("CONFIG_FILE", env_ex, "relay.env.example 未提及 CONFIG_FILE")

    def test_http_status_codes_match_implementation(self):
        """README 的 HTTP 契约表必须覆盖 server.py 实际返回的每一个状态码。

        手写状态码列表必然漂移 —— 之前就漏了 5 个（404 / 400 bad-length /
        413 / 400 missing-delivery / 200 ignored），而没有任何东西会发现。
        """
        with open(os.path.join(ROOT, "relay", "server.py"), encoding="utf-8") as f:
            src = f.read()
        implemented = {int(c) for c, _ in re.findall(r'_reply\((\d+),\s*"([\w-]+)"', src)}

        with open(README, encoding="utf-8") as f:
            rd = f.read()
        contract = rd[rd.index("## HTTP 契约"):rd.index("### 去重与重试语义")]
        documented = {int(c) for c in re.findall(r'`(\d{3})', contract)}

        self.assertTrue(documented, "契约表里一个状态码都没解析到")
        missing = implemented - documented
        self.assertEqual(missing, set(),
                         f"server.py 返回了但 README 契约表没写: {sorted(missing)}")

    def test_every_reply_has_a_documented_body(self):
        """契约表里的每个 body 文案也要对得上，否则平台侧排查会摸不着头脑。"""
        with open(os.path.join(ROOT, "relay", "server.py"), encoding="utf-8") as f:
            src = f.read()
        bodies = _reply_bodies(src)
        self.assertIn("accepted", bodies, "没抓到响应体，扫描逻辑坏了")

        with open(README, encoding="utf-8") as f:
            rd = f.read()
        contract = rd[rd.index("## HTTP 契约"):rd.index("### 去重与重试语义")]
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
