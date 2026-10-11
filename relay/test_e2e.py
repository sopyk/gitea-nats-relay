"""端到端仿真测试：真 HTTP、真 NATS、真订阅端。

为什么单独一个文件：前三组测的是**日志正确性**，必须打真实的 socket
（伪造 Handler 实例测不出 keep-alive 污染）；最后一组测的是**产品核心**
—— 消息到底有没有到达订阅者。

之前一直没验过最后一件事：日志写着 `status=200 decision=accepted`，
只能证明 relay 认为它发出去了，证明不了订阅者真收到了。而当初做日志
就是因为「什么都查不到」，那如果「查到的」不准，比查不到更糟。

## 订阅端是手写的裸 socket，不用 nats-py

三个理由：
1. 零依赖 —— 这台机器没装 nats-py 也能跑
2. 验的是**线上真实协议字节**，不是客户端库的二次封装
3. 顺带证明 relay 的输出对「不是 nats-py 的订阅者」也成立
   （另一个团队的 Agent 大概率不会用 nats-py）

## 怎么跑

```bash
cd relay && python3 test_e2e.py            # 有 docker 就自动起 NATS
W2N_NATS=nats://127.0.0.1:4222 python3 test_e2e.py   # 或指定已有服务
```

没有 docker 也没有 W2N_NATS 时，真实 NATS 那组会 **skip**（不是静默通过）。
"""
import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as config_mod  # noqa: E402
import core  # noqa: E402
import fixtures  # noqa: E402
import logs as logs_mod  # noqa: E402
import providers as providers_mod  # noqa: E402
import server as server_mod  # noqa: E402

SECRET = "e2e-secret"
NATS_CONTAINER = "w2n-e2e-nats"
NATS_HOST_PORT = 14222

# 注意：core.Members 收的是**内层**映射（members.json 里的 members 字段），
# 不是整个文件。多包一层的话 resolve() 全部返回 None，于是每个事件都算出
# 零个 subject —— 而 relay 仍然会回 accepted（发了个寂寞）。
# 第一版就踩了这个坑，症状是「订阅端一条都收不到」，和真正的投递故障
# 长得一模一样。
MEMBERS = {
    "alice": {"host": "h1", "id": "alice"},
    "bob": {"host": "h1", "id": "bob"},
    "carol": {"host": "h2", "id": "carol"},
}


# ────────────────────────── 极简 NATS 客户端 ──────────────────────────

class MiniNatsClient:
    """只够本测试用的 NATS 订阅端（裸 TCP，核心 NATS 协议）。

    协议只有这几步：服务端先发 INFO，客户端回 CONNECT，再 SUB，然后收 MSG。
    中间服务端会周期性 PING，必须回 PONG，否则连接会被服务端断开 ——
    漏掉这一步的话测试会因为「等不到消息」而失败，症状和真正的 bug
    长得一模一样，所以这里认真处理。
    """

    def __init__(self, url, user=None, password=None, timeout=5.0):
        m = re.match(
            r"nats://(?:(?P<user>[^:@]+):(?P<pw>[^@]*)@)?"
            r"(?P<host>[^:]+):(?P<port>\d+)", url)
        if not m:
            raise ValueError(f"看不懂的 NATS 地址: {url}")
        self.sock = socket.create_connection(
            (m.group("host"), int(m.group("port"))), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._sid = 0
        self._deadline = time.monotonic() + timeout

        self._readline()                      # INFO
        conn = {"verbose": False, "pedantic": False, "name": "w2n-e2e"}
        if m.group("user"):
            conn["user"] = m.group("user")
            conn["pass"] = m.group("pw")
        self._send("CONNECT " + json.dumps(conn))
        self._send("PING")
        self._expect("PONG")

    def _send(self, line):
        self.sock.sendall((line + "\r\n").encode())

    def _fill(self):
        self.sock.settimeout(max(0.05, self._deadline - time.monotonic()))
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("NATS 连接被服务端关闭")
        self._buf += chunk

    def _readline(self):
        while b"\r\n" not in self._buf:
            self._fill()
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line.decode("utf-8", "replace")

    def _expect(self, prefix):
        while True:
            line = self._readline()
            if line.startswith(prefix):
                return line
            if line.startswith("-ERR"):
                raise RuntimeError(f"NATS 拒绝连接: {line}")

    def subscribe(self, subject):
        """返回 sid。**必须在触发事件之前调用** —— Core NATS 不留存，
        发布那一刻没人在听，消息就永久消散了。

        这里**不能**等 `+OK`：那个确认只在 verbose 模式下发送，而我们用
        verbose=false（少一轮往返）。改成发完 SUB 立刻 PING、等 PONG ——
        NATS 按序处理命令，收到 PONG 就说明 SUB 已经生效，订阅确实在线。
        """
        self._sid += 1
        self._send(f"SUB {subject} {self._sid}")
        self._send("PING")
        self._expect("PONG")
        return self._sid

    def drain(self, timeout=2.0):
        """收满 timeout 为止的所有消息，返回 [(subject, payload_str)]。"""
        got = []
        self._deadline = time.monotonic() + timeout
        while True:
            try:
                line = self._readline()
            except (socket.timeout, TimeoutError, OSError):
                break
            if line.startswith("PING"):
                self._send("PONG")
                continue
            if line.startswith("PONG"):
                continue
            if not line.startswith("MSG "):
                continue
            parts = line.split()
            size = int(parts[-1])
            while len(self._buf) < size + 2:      # payload + CRLF
                try:
                    self._fill()
                except (socket.timeout, TimeoutError, OSError):
                    break
            payload, self._buf = self._buf[:size], self._buf[size + 2:]
            got.append((parts[1], payload.decode("utf-8", "replace")))
            if len(got) > 500:                    # 防御：别把内存吃光
                break
        return got

    def close(self):
        try:
            self._send("PING")
            self.sock.close()
        except OSError:
            pass


# ─────────────────────── 起一个真 NATS ───────────────────────

def _docker(*args, timeout=120):
    """跑 docker 命令。

    先试普通 docker，不行就退回 `sudo -n docker` —— 很多机器（尤其
    开发机）docker 只对 root 可用，而测试不该因为这个就静默跳过，
    更不该在没有说明的情况下降级成「假装测过了」。
    """
    last = None
    for cmd in (["docker"], ["sudo", "-n", "docker"]):
        if shutil.which(cmd[0]) is None:
            continue
        try:
            r = subprocess.run(cmd + list(args), capture_output=True,
                               timeout=timeout)
            if r.returncode == 0:
                return r
            last = (r.stderr or b"").decode("utf-8", "replace")[:200]
        except (subprocess.SubprocessError, OSError) as e:
            last = repr(e)
    raise RuntimeError(f"docker {' '.join(args)} 失败: {last}")


class NatsServer:
    """按需拉起一个真 nats-server（docker）。拉不起来就让测试 skip。

    支持按名字/端口起多份 —— 有一组测试需要「中途把 NATS 停掉」，
    不能连带把别的用例正在用的那份也停掉。
    """

    _procs = {}

    def __init__(self, container, port):
        self.container = container
        self.port = port

    @property
    def url(self):
        return f"nats://127.0.0.1:{self.port}"

    def start(self):
        key = self.container
        if key in self._procs:
            return self._procs[key]
        if not (shutil.which("docker") or shutil.which("sudo")):
            raise RuntimeError("没有 docker，也没有 sudo")
        try:
            try:
                _docker("rm", "-f", self.container)
            except RuntimeError:
                pass                      # 容器本来就不存在，正常
            _docker("run", "-d", "--name", self.container,
                    "-p", f"{self.port}:4222", "nats:2.10-alpine")
        except RuntimeError as e:
            raise RuntimeError(f"起 NATS 容器失败: {e}") from None
        # 等**真的**能连上，而不是等 TCP 端口开。
        #
        # docker 的端口转发会在容器进程绑定端口之前就接受 TCP 连接，
        # 所以「socket 连得上」不等于 NATS 已经 ready —— 第一版就死在这，
        # 症状是同一份代码有时通过有时 skip，非常难查。
        # 必须走一次真正的 NATS 握手（读 INFO + PING/PONG）。
        last = None
        for _ in range(60):
            try:
                c = MiniNatsClient(self.url, timeout=1.0)
                c.close()
                self._procs[key] = self.url
                return self.url
            except (OSError, ConnectionError, RuntimeError) as e:
                last = e
                time.sleep(0.2)
        self.stop()
        raise RuntimeError(f"容器起来了但 NATS 握手失败（{last}）")

    def stop(self):
        try:
            _docker("rm", "-f", self.container)
        except RuntimeError:
            pass
        self._procs.pop(self.container, None)

    @classmethod
    def cleanup_all(cls):
        for container in list(cls._procs):
            try:
                _docker("rm", "-f", container)
            except RuntimeError:
                pass
        cls._procs.clear()


NATS_MAIN = lambda: NatsServer(NATS_CONTAINER, NATS_HOST_PORT)  # noqa: E731
NATS_PF = lambda: NatsServer("w2n-e2e-nats-pf", 14223)       # noqa: E731


def tearDownModule():
    """整个模块跑完再清容器。

    刻意不放进各个类的 tearDownClass：容器是共享的，谁先 tearDown 谁
    就把后面还没跑的类连累成 skip。
    """
    NatsServer.cleanup_all()


def _nats_or_skip(which=NATS_MAIN):
    """拿一个可用的 NATS 地址；起不来就 skip（而不是假装测过了）。"""
    env = os.environ.get("W2N_NATS")
    if env and which is NATS_MAIN:
        return env
    try:
        url = which().start()
    except RuntimeError as e:
        raise unittest.SkipTest(
            f"没有可用的 NATS（{e}）：装 docker，或设 W2N_NATS=nats://host:port")
    try:
        c = MiniNatsClient(url)
        c.close()
    except OSError as e:
        raise unittest.SkipTest(f"NATS 连不上（{e}）")
    return url


# ─────────────────────── HTTP 测试脚手架 ───────────────────────

class FakeRelay:
    """假 NATS 发布端 —— 只用于与 NATS 无关的日志测试。

    publish_fn 可注入，用于制造发布失败/超时分支。
    """

    def __init__(self, publish_fn=None):
        self.nc = None
        self.published = []
        self._publish_fn = publish_fn or (lambda ev: [])

    def submit_event(self, ev):
        relay = self

        class F:
            def result(self, timeout=None):
                if timeout is not None:
                    time.sleep(0)      # 立刻返回，不占 HTTP 线程
                return relay._publish_fn(ev)
        self.published.append(ev)
        return F()

    def healthz(self):
        return True


class HttpCase(unittest.TestCase):
    """真 HTTP server + 内存日志捕获。"""

    def setUp(self):
        self.stream = io.StringIO()
        logs_mod.setup(config_mod.LogConfig(), stream=self.stream)
        self.tmp = tempfile.mkdtemp()
        self.relay = FakeRelay()
        self.app = self._make_app()
        self.httpd = server_mod.serve(self.app, "127.0.0.1:0")
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        logs_mod.setup(config_mod.LogConfig())

    def _make_app(self):
        cfg = config_mod.load(path=None, env={
            "NATS_SERVERS": "nats://x:4222", "NATS_USER": "u",
            "NATS_PASSWORD": "p", "WEBHOOK_SECRET": SECRET})
        return server_mod.App(
            cfg, providers_mod.build_registry(cfg), core.Members(MEMBERS),
            core.Seen(os.path.join(self.tmp, "seen.json"), 100), self.relay)

    # ── 请求构造 ──
    def signed(self, payload, delivery, event="issue_comment", sig=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        signature = sig or hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return (f"POST /gitea-hook HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"X-Gitea-Delivery: {delivery}\r\n"
                f"X-Gitea-Signature: {signature}\r\n"
                f"X-Gitea-Event: {event}\r\n\r\n").encode() + body

    def send_all(self, requests):
        """多个请求走**同一条 TCP 连接** —— keep-alive 是本文件的主角。"""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        out = []
        try:
            s.settimeout(5)
            for req in requests:
                try:
                    s.sendall(req)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    out.append((None, b""))
                    break
                head, _, rest = buf.partition(b"\r\n\r\n")
                n = int(re.search(rb"Content-Length:\s*(\d+)", head, re.I)
                        .group(1))
                while len(rest) < n:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
                out.append((int(head.split(b" ")[1]), rest[:n] if n else b""))
                if b"Connection: close" in head:
                    break
            return out
        finally:
            s.close()

    def log_lines(self):
        return [l for l in self.stream.getvalue().splitlines() if l.strip()]

    def line_for(self, delivery):
        return next((l for l in self.log_lines() if f"delivery={delivery}" in l),
                    None)


def _read_server():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "server.py"), encoding="utf-8") as f:
        return f.read()


def _payload(repo, actor, number, comment):
    p = fixtures.issue_comment(body=comment)
    p["repository"]["full_name"] = repo
    p["sender"]["login"] = actor
    p["issue"]["number"] = number
    return p


# ────────────────────── 1. keep-alive 字段污染 ──────────────────────

class KeepAliveLeakTest(HttpCase):
    """同一个 Handler 实例会处理 keep-alive 上的多个请求。

    `setup()` 只在建连时调一次，所以**每请求**的日志字段必须在每个请求
    入口重置 —— 忘了重置的话，_mark() 对提前返回的分支不覆盖的字段
    （repo / actor / number / mentions / event / kind）会串到下一个请求。

    这是实测出来的，不是假想：同一条连接「正常投递 → 401 → 404」，
    401 那行挂着上一个合法请求的仓库和作者，404 那行还带着
    `provider=gitea`（而 404 意味着压根没匹配到 provider）。
    """

    def test_401_does_not_inherit_previous_request(self):
        good = _payload("SECRET-REPO", "secret-actor", 999, "@alice 重要")
        body = json.dumps(good, ensure_ascii=False).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.send_all([
            self.signed(good, "real-1"),
            self.signed(good, "attack-1", sig="WRONG"),
        ])
        line = self.line_for("attack-1")
        self.assertIsNotNone(line, "401 没有留下日志行")
        for leak in ("SECRET-REPO", "secret-actor", "number=999", "mentions="):
            self.assertNotIn(leak, line,
                             f"401 行泄漏了上一个请求的字段 {leak!r}：{line}")

    def test_404_does_not_inherit_provider_or_event(self):
        good = _payload("repo-x", "actor-x", 5, "@bob 看")
        self.send_all([
            self.signed(good, "ok-1"),
            b"POST /nope HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}",
        ])
        line = next((l for l in self.log_lines() if "decision=not-found" in l), None)
        self.assertIsNotNone(line, "404 没有留下日志行")
        # 404 意味着没匹配到 provider，所以 provider / kind / event
        # 全都不该出现 —— 出现了就说明是上一个请求的残留
        for leak in ("provider=", "kind=", "event=", "repo=", "actor="):
            self.assertNotIn(leak, line,
                             f"404 行带着不该有的字段 {leak!r}：{line}")

    def test_three_events_on_one_connection_stay_separate(self):
        cases = [("repo-A", "alice", 1, "d-A"), ("repo-B", "bob", 2, "d-B"),
                 ("repo-C", "carol", 3, "d-C")]
        self.send_all([self.signed(_payload(r, a, n, f"@{a} 看一下"), d)
                       for r, a, n, d in cases])
        for repo, actor, num, dlv in cases:
            line = self.line_for(dlv)
            self.assertIsNotNone(line, f"{dlv} 没有日志行")
            self.assertIn(f"repo={repo}", line)
            self.assertIn(f"actor={actor}", line)
            self.assertIn(f"number={num}", line)
            for other_repo, _, _, _ in cases:
                if other_repo != repo:
                    self.assertNotIn(
                        f"repo={other_repo}", line,
                        f"{dlv} 的日志里混进了 {other_repo}：{line}")

    def test_field_reset_survives_a_later_get(self):
        """GET 也要重置 —— 否则 /healthz 会挂着上一个 webhook 的字段。"""
        self.send_all([
            self.signed(_payload("repo-get", "actor-get", 7, "@alice"), "g-1"),
            b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n",
        ])
        line = next((l for l in self.log_lines() if "/healthz" in l), None)
        self.assertIsNotNone(line, "healthz 没有日志行")
        for leak in ("repo-get", "actor-get", "mentions="):
            self.assertNotIn(leak, line, f"healthz 泄漏了 {leak!r}：{line}")

    def test_every_request_entry_resets_state(self):
        """结构性护栏：do_GET / do_POST 都必须调 _begin_request()。

        行为测试已经覆盖了，但它依赖「碰巧有一个字段不重叠」。
        这条直接钉住调用点 —— 将来有人加一个新入口忘了调，会被抓住。
        """
        import ast
        tree = ast.parse(_read_server())
        for name in ("do_GET", "do_POST"):
            node = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == name)
            calls = {ast.unparse(s.func) for s in ast.walk(node)
                     if isinstance(s, ast.Call)}
            self.assertIn("self._begin_request", calls,
                          f"{name} 没有调 _begin_request()，"
                          f"keep-alive 下日志字段会串到下一个请求")

    def test_reverse_check_detects_a_removed_reset(self):
        """反向自检：把重置拿掉，行为测试必须失败。

        没有这条，上面几条可能因为「测试根本没跑到那条路径」而永远绿
        —— 上一轮的 mutation 就是这么漏掉 4 个的。
        """
        src = _read_server()
        anchor = "self._begin_request()\n        route = self._route()"
        self.assertEqual(src.count(anchor), 2,
                         "锚点变了 —— do_GET/do_POST 的入口写法改了，"
                         "请同步这个反向自检")
        broken = src.replace(anchor, "route = self._route()", 1)
        self.assertEqual(broken.count(anchor), 1,
                         "替换没有生效（do_GET 的那处应该被拿掉了）")
        # 而剩下的那处是 do_POST 的，所以「少了一个」本身就是信号
        self.assertLess(broken.count("self._begin_request()"),
                        src.count("self._begin_request()"))


# ────────────────────── 2. 日志注入 ──────────────────────

class LogInjectionTest(HttpCase):
    """一个值里的换行不能让一条日志裂成两条。

    裂缝一旦存在，攻击者就能往日志里写任意内容 —— 而日志正是排障时
    唯一可信的东西。被伪造的一行 `decision=accepted status=200` 能让
    运维相信一次根本没发生的投递。

    可达向量（实测确认能一路走到日志字段）：
      · `log_bodies` 打开时的评论正文 —— **任何能评论的人**都能控制
      · payload 里的 `repo` / `actor` —— 伪造/被攻陷的平台可控制
      · `error=repr(e)` —— 异常消息里带换行很常见

    不可达：HTTP 头值。裸 CRLF 在协议层就是头分隔符，压根到不了应用。
    之前那条基于 delivery 头的测试是写错了方向 —— 它「通过」只是因为
    请求根本没发出去。
    """

    FORGED = "repo=FAKE status=200 decision=accepted msg=\"伪造的投递\""

    def _assert_no_forgery(self, why):
        lines = [l for l in self.log_lines() if "decision=" in l]
        forged = [l for l in lines if "repo=FAKE" in l and "msg=\"伪造的投递\"" in l]
        self.assertEqual(forged, [],
                         f"{why}：注入成功，凭空多出一条日志 {forged}")
        # 真请求的日志必须还在，且换行被转义成字面量而不是真的断开
        self.assertTrue(lines, f"{why}：连真实请求的日志都没了")
        return lines

    def test_repo_field_with_newline_cannot_forge_a_log_line(self):
        p = _payload("acme/w2n", "alice", 1, "@alice 看下")
        p["repository"]["full_name"] = f"acme/w2n\n{self.FORGED}"
        self.send_all([self.signed(p, "inj-1")])
        lines = self._assert_no_forgery("repo 字段带换行")
        # 值里有空格 → 加引号；换行 → 字面量 \n。伪造内容整段被关在引号内，
        # 真正的 msg= 与 status= 字段毫发无损。
        self.assertIn('repo="acme/w2n\\n', lines[0],
                      "换行没有被转义成字面量")
        self.assertEqual(lines[0].count("\n"), 0, "日志行被劈开了")
        # 注意不能断言 "status=200" 只出现一次 —— 注入内容本身就带着
        # 一个 status=200，它在引号里，是**数据**不是字段。能断言的是
        # 真正的字段仍在行尾且值没被污染。
        self.assertTrue(lines[0].rstrip().endswith('msg="请求处理完成"'),
                        f"真正的 msg 字段被污染了：{lines[0]}")
        self.assertIn(" dur_ms=", lines[0])
        self.assertIn(" status=200 decision=accepted peer=", lines[0])

    def test_actor_field_with_newline_cannot_forge_a_log_line(self):
        p = _payload("acme/w2n", "alice", 1, "@alice 看下")
        p["sender"]["login"] = f"alice\n{self.FORGED}"
        self.send_all([self.signed(p, "inj-2")])
        self._assert_no_forgery("actor 字段带换行")

    def enable_log_bodies(self):
        """打开正文逃生开关。

        必须**同时**改 cfg 和重跑 logs.setup()，而且要用**同一个** cfg
        对象 —— 真实代码里 amain() 就是 `logs_mod.setup(cfg.logging)`。
        只改 cfg 的话，logs 模块里的进程级开关还是关的，正文会被
        纵深防御拦掉（这正是它该做的，只是会让测试以为开关坏了）。
        """
        self.app.cfg.logging.log_bodies = True
        logs_mod.setup(self.app.cfg.logging, stream=self.stream)

    def test_comment_body_is_the_real_attack_surface(self):
        """log_bodies 打开时，任何能评论的人都能控制日志内容。"""
        self.enable_log_bodies()
        p = _payload("acme/w2n", "alice", 1,
                     f"@alice 正常诉求\n{self.FORGED}")
        self.send_all([self.signed(p, "inj-3")])
        line = self.line_for("inj-3")
        self.assertIsNotNone(line, "日志行不见了")
        self.assertEqual(line.count("\n"), 0, "正文里的换行把日志劈开了")
        # 这个开关记的是**完整原始 payload**（见 _process 的注释与 README），
        # 所以评论正文一定在里面 —— 这正是它能用来排查解析 bug 的原因。
        self.assertIn("正常诉求", line, "逃生开关打开后原始 payload 应该出现")
        self.assertIn('body="', line, "原始 payload 没有记进 body 字段")
        self.assertNotIn('msg="伪造的投递"', line,
                         "伪造内容逃出了字段值的引号")
        self.assertIn('msg="请求处理完成"', line, "真正的 msg 字段被污染了")

    def test_escaping_is_written_into_the_value_not_the_record(self):
        """直接打格式化层：这条不依赖任何 HTTP 行为，边界最清楚。"""
        stream = io.StringIO()
        logs_mod.setup(config_mod.LogConfig(), stream=stream)
        try:
            logs_mod.event(logs_mod.get_logger(), logging.INFO, "x",
                           note="第一行\n第二行")
        finally:
            logs_mod.setup(config_mod.LogConfig())
        out = stream.getvalue()
        self.assertEqual(out.count("\n"), 1, "转义后仍应只有一行")
        self.assertIn("第一行\\n第二行", out)
        self.assertNotIn("第二行", out.replace("第一行\\n第二行", ""))

    def test_no_log_line_ever_contains_a_raw_newline_payload(self):
        """整体性质：一条记录永远只占一行。"""
        cases = ["a\nb", "a\rb", "a\r\nb", "a\x00b", "a\x1b[31mred", "\x08退格"]
        for idx, evil in enumerate(cases):
            before = len(self.log_lines())
            p = _payload("acme/w2n", "alice", 1, "@alice x")
            p["sender"]["login"] = evil
            self.send_all([self.signed(p, f"inj-{idx}")])
            new = self.log_lines()[before:]
            self.assertEqual(len(new), 1,
                             f"payload {evil!r} 产生了 {len(new)} 行日志：{new}")
            for line in new:
                self.assertNotIn("\r", line,
                                 f"payload {evil!r} 产生了带 CR 的行：{line!r}")
                self.assertNotIn("\x00", line, f"payload {evil!r} 产生了 NUL")
                self.assertNotIn("\x1b", line,
                                 f"payload {evil!r} 残留了 ESC 控制符")
                self.assertNotIn("\x08", line, f"payload {evil!r} 残留了退格符")
                self.assertEqual(line.count("\n"), 0,
                                 f"payload {evil!r} 把日志行劈开了")

    def test_ansi_escape_is_neutralised(self):
        """ESC 能改终端颜色、清屏、伪造提示符，在 docker logs 上是实打实的操纵。"""
        self.enable_log_bodies()
        p = _payload("acme/w2n", "alice", 1, "@alice \x1b[2J\x1b[31m伪造")
        self.send_all([self.signed(p, "esc-1")])
        for line in self.log_lines():
            self.assertNotIn("\x1b", line, f"日志里残留了 ESC：{line!r}")

    def test_setup_keeps_the_module_switch_in_sync_with_the_config(self):
        """钉住那个隐式耦合：cfg 和 logs 模块里各有一份开关。

        生产里 amain() 传的是同一个 cfg 对象，所以两者必然一致；但那是
        「调用方恰好做对了」，不是「代码保证的」。这里钉死 —— 将来有人
        改 setup() 的调用方式导致只更新了一半，正文就会静默不出现，
        而现象是「开关配了但没效果」，极难自查。
        """
        cfg = config_mod.LogConfig(log_bodies=True)
        logs_mod.setup(cfg, stream=io.StringIO())
        try:
            self.assertTrue(logs_mod._ALLOW_BODIES,
                            "setup() 之后模块开关与 cfg 不一致")
            self.assertFalse(
                logs_mod.safe_fields({"body": "x"}, allow_bodies=False))
            self.assertEqual(
                logs_mod.safe_fields({"body": "x"}, allow_bodies=True),
                {"body": "x"})
        finally:
            logs_mod.setup(config_mod.LogConfig())
        self.assertFalse(logs_mod._ALLOW_BODIES,
                         "恢复默认配置后开关没有归位")

    def test_secret_is_still_blocked_even_with_the_escape_hatch_on(self):
        """逃生开关只放行正文，绝不能顺带放行密钥。"""
        stream = io.StringIO()
        logs_mod.setup(config_mod.LogConfig(log_bodies=True), stream=stream)
        try:
            logs_mod.event(logs_mod.get_logger(), logging.INFO, "x",
                           body="正文", secret="s3cr3t", password="hunter2")
        finally:
            logs_mod.setup(config_mod.LogConfig())
        out = stream.getvalue()
        self.assertIn("正文", out, "逃生开关该放行正文")
        self.assertNotIn("s3cr3t", out, "密钥不该被放行")
        self.assertNotIn("hunter2", out, "口令不该被放行")

    def test_long_values_are_truncated_not_dropped(self):
        """超长值要截断，但不能因此把整行丢掉或换行。"""
        p = _payload("r" * 5000, "alice", 1, "@alice")
        self.send_all([self.signed(p, "long-1")])
        line = self.line_for("long-1")
        self.assertIsNotNone(line, "超长字段不该让整条日志消失")
        self.assertEqual(line.count("\n"), 0)
        self.assertLess(len(line), 2000, "超长字段没有被截断")


# ────────────────────── 3. 堆栈可见 ──────────────────────

class StackTraceTest(HttpCase):
    """text 是**默认**格式，它必须把堆栈也打出来。

    之前的 TextFormatter 漏了 exc_info：未预期异常只留下一行
    `msg="未处理的异常"`，没有任何堆栈 —— 恰好在最需要堆栈的时候
    什么都看不到。
    """

    def test_text_format_includes_the_traceback(self):
        stream = io.StringIO()
        logs_mod.setup(config_mod.LogConfig(), stream=stream)
        try:
            logger = logs_mod.get_logger()
            try:
                raise ValueError("模拟的内部错误")
            except ValueError:
                logs_mod.event(logger, logging.ERROR, "未处理的异常",
                               layer="_process", exc_info=True)
        finally:
            logs_mod.setup(config_mod.LogConfig())
        out = stream.getvalue()
        self.assertIn("exc=", out, "text 格式没有输出堆栈")
        self.assertIn("ValueError", out)
        self.assertIn("模拟的内部错误", out)
        self.assertIn("Traceback", out)

    def test_traceback_stays_on_one_line(self):
        """堆栈有多行，转义之后仍必须是一条日志一行。"""
        stream = io.StringIO()
        logs_mod.setup(config_mod.LogConfig(), stream=stream)
        try:
            logger = logs_mod.get_logger()
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                logs_mod.event(logger, logging.ERROR, "x", exc_info=True)
        finally:
            logs_mod.setup(config_mod.LogConfig())
        for line in stream.getvalue().splitlines():
            self.assertTrue(line.strip(), "不该有空行")
            self.assertNotIn("\t", line, "堆栈里的制表符没转义")

    def test_json_format_still_works_with_traceback(self):
        stream = io.StringIO()
        logs_mod.setup(config_mod.LogConfig(format="json"), stream=stream)
        try:
            logger = logs_mod.get_logger()
            try:
                raise KeyError("k")
            except KeyError:
                logs_mod.event(logger, logging.ERROR, "x", exc_info=True)
        finally:
            logs_mod.setup(config_mod.LogConfig())
        lines = [l for l in stream.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, "JSON 格式下堆栈不能把一行拆成多行")
        doc = json.loads(lines[0])
        self.assertIn("exc", doc)
        self.assertIn("KeyError", doc["exc"])

    def test_real_500_still_reaches_the_client(self):
        """堆栈打出来了，但不能因此让响应变成连接重置。

        这正是当初 `logger.exception(msg, layer=...)` 那个 bug 的症状：
        层号被当成 logging 的 kwarg，Logger._log() 抛 TypeError，
        而那行代码就在异常处理器里 —— 处理器自己一抛，客户端什么都没拿到。
        """
        app = self.app
        original = app.registry["/gitea-hook"].normalizer
        app.registry["/gitea-hook"].normalizer = (
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            status, body = self.send_all(
                [self.signed(_payload("r", "a", 1, "@alice"), "boom-1")])[0]
        finally:
            app.registry["/gitea-hook"].normalizer = original
        self.assertEqual(status, 500, "未预期异常必须回诚实的 500，而不是断连")
        self.assertEqual(json.loads(body)["error"], "publish-error")
        # 堆栈那一行必须**带 delivery**，否则按 delivery 查堆栈查不到，
        # 整个日志功能的承诺就落空了
        stack_lines = [l for l in self.log_lines()
                       if "boom-1" in l and "exc=" in l]
        self.assertTrue(stack_lines,
                        f"500 的堆栈行没有带 delivery，无法与请求关联："
                        f"{self.log_lines()}")
        self.assertIn("RuntimeError", stack_lines[0])
        self.assertIn("layer=_process", stack_lines[0])
        # 而且 delivery 不能卡在 inflight
        self.assertNotIn("boom-1", self.app.seen.inflight)

    def test_stack_line_carries_full_request_context(self):
        """堆栈行要能看出是哪个仓库、哪次提及出错，而不只是 delivery。"""
        app = self.app
        original = app.registry["/gitea-hook"].normalizer
        app.registry["/gitea-hook"].normalizer = (
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ctx-boom")))
        try:
            self.send_all([self.signed(
                _payload("acme/ctxrepo", "ctxactor", 42, "@alice 求助"), "ctx-1")])
        finally:
            app.registry["/gitea-hook"].normalizer = original
        stack = [l for l in self.log_lines() if "exc=" in l]
        self.assertTrue(stack, "没有堆栈行")
        # normalize 在这里就炸了，所以 repo/actor 可能还没进字段 ——
        # 关键是 delivery 必须有，这一条不可省
        self.assertIn("delivery=ctx-1", stack[0])


# ────────────────────── 4. 并发 ──────────────────────

class ConcurrencyTest(HttpCase):
    """ThreadingHTTPServer 是多线程的，日志状态不能互相踩。

    每个 Handler 实例是每连接一份，所以字段本身不共享；真正共享的是
    logger 本身、RateLimiter 和 logging 的 handler 锁。
    """

    def test_parallel_requests_produce_one_clean_line_each(self):
        n = 40
        results = [None] * n
        reqs = [self.signed(_payload(f"repo-{i}", f"user-{i}", i,
                                     f"@alice 第 {i} 条"), f"par-{i}")
                for i in range(n)]

        def worker(i):
            results[i] = self.send_all([reqs[i]])[0]

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertTrue(all(r is not None for r in results), "有请求没拿到响应")
        for i, (status, _body) in enumerate(results):
            self.assertEqual(status, 200, f"第 {i} 个请求状态码是 {status}")

        lines = [l for l in self.log_lines() if "decision=accepted" in l]
        self.assertEqual(len(lines), n, f"应有 {n} 条投递日志，实际 {len(lines)} 条")

        # 并发下日志行的**顺序**和提交顺序无关（这是对的，HTTP 线程各自
        # 独立），所以必须按 delivery 建映射逐条核对，而不是按下标。
        by_delivery = {}
        for line in lines:
            m = re.search(r"delivery=(\S+)", line)
            self.assertIsNotNone(m, f"日志行里没有 delivery：{line}")
            dlv = m.group(1)
            self.assertNotIn(dlv, by_delivery,
                             f"delivery={dlv} 出现了两条日志：{line}")
            by_delivery[dlv] = line
        for i in range(n):
            dlv = f"par-{i}"
            self.assertIn(dlv, by_delivery, f"少了 delivery={dlv} 的日志")
            line = by_delivery[dlv]
            self.assertIn(f"repo=repo-{i}", line,
                          f"{dlv} 的 repo 字段串了：{line}")
            self.assertIn(f"actor=user-{i}", line,
                          f"{dlv} 的 actor 字段串了：{line}")
            self.assertEqual(line.count("delivery="), 1,
                             f"一行里有多个 delivery：{line}")

    def test_concurrent_401_does_not_blow_up_the_limiter(self):
        """401 限流器在多线程下必须只放行固定条数，且不能抛异常。"""
        app = self.app

        def worker(i):
            try:
                self.send_all([self.signed(_payload("r", "a", i, "@alice"),
                                           f"c401-{i}", sig="WRONG")])
            except OSError:
                pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        detailed = [l for l in self.log_lines() if "验签失败" in l
                    and "已限流" not in l]
        self.assertLessEqual(
            len(detailed), 5,
            f"限流器在并发下失效了：{len(detailed)} 条明细（应 ≤5）")
        self.assertIsNotNone(app.bad_sig_limiter)


# ────────────────────── 5. 真实 NATS 全链路 ──────────────────────

@unittest.skipUnless(shutil.which("docker") or os.environ.get("W2N_NATS"),
                     "需要 docker 或 W2N_NATS")
class RealNatsEndToEndTest(unittest.TestCase):
    """**本项目的核心功能**：relay 发的消息，订阅端到底收不收得到。

    之前只验到「日志写着 accepted」—— 那只能证明 relay 认为它发出去了。
    Core NATS 不留存，订阅者必须先在线，所以这里严格按
    「先订阅 → 确认在线 → 再触发」的顺序，并把这条顺序本身也测一遍。
    """

    @classmethod
    def setUpClass(cls):
        cls.url = _nats_or_skip()
        cls.sub = MiniNatsClient(cls.url)
        cls.sub.subscribe("evt.>")      # 必须在触发事件之前
        cls.tmp = tempfile.mkdtemp()
        cls.cfg = config_mod.load(path=None, env={
            "NATS_SERVERS": cls.url, "NATS_USER": "e2e", "NATS_PASSWORD": "e2e",
            "WEBHOOK_SECRET": SECRET, "PUBLISH_TIMEOUT_SECONDS": "3"})
        cls.relay = _RealRelay(cls.cfg, core.Members(MEMBERS))
        cls.relay.start()
        cls.app = server_mod.App(
            cls.cfg, providers_mod.build_registry(cls.cfg), core.Members(MEMBERS),
            core.Seen(os.path.join(cls.tmp, "seen.json"), 100), cls.relay)
        cls.httpd = server_mod.serve(cls.app, "127.0.0.1:0")
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        finally:
            cls.relay.stop()
            cls.sub.close()

    def post(self, payload, delivery):
        body = json.dumps(payload, ensure_ascii=False).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        req = (f"POST /gitea-hook HTTP/1.1\r\nHost: x\r\n"
               f"Content-Length: {len(body)}\r\nX-Gitea-Delivery: {delivery}\r\n"
               f"X-Gitea-Signature: {sig}\r\nX-Gitea-Event: issue_comment\r\n"
               f"\r\n").encode() + body
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s.sendall(req)
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += s.recv(65536)
            head, _, rest = buf.partition(b"\r\n\r\n")
            n = int(re.search(rb"Content-Length:\s*(\d+)", head, re.I).group(1))
            while len(rest) < n:
                rest += s.recv(65536)
            return int(head.split(b" ")[1]), json.loads(rest[:n])
        finally:
            s.close()

    def test_message_actually_reaches_the_subscriber(self):
        self.sub.drain(0.3)                     # 清掉上一条测试的残留
        status, doc = self.post(
            _payload("acme/w2n", "zhangsan", 134, "@alice 请处理"), "e2e-1")
        self.assertEqual(status, 200)
        self.assertEqual(doc["status"], "accepted")

        msgs = self.sub.drain(2.0)
        self.assertTrue(msgs, "relay 回了 accepted，但订阅端一条都没收到")
        subject, raw = msgs[0]
        self.assertEqual(subject, "evt.gitea.notify.h1.alice",
                         f"订阅端收到的 subject 不对：{subject}")
        ev = json.loads(raw)
        self.assertEqual(ev["delivery"], "e2e-1")
        self.assertEqual(ev["repo"], "acme/w2n")
        self.assertEqual(ev["number"], 134)
        self.assertEqual(ev["actor"], "zhangsan")
        self.assertIn("alice", ev["mentions"])

    def test_delivery_in_log_matches_delivery_in_message(self):
        """日志与消息靠 delivery join —— 这是整个日志功能的承诺。"""
        import io as _io
        stream = _io.StringIO()
        logs_mod.setup(config_mod.LogConfig(), stream=stream)
        try:
            self.post(_payload("acme/w2n", "lisi", 7, "@bob 看看"), "join-xyz")
            self.sub.drain(2.0)
        finally:
            logs_mod.setup(config_mod.LogConfig())
        lines = [l for l in stream.getvalue().splitlines() if "join-xyz" in l]
        self.assertTrue(lines, "日志里没有 join-xyz")
        self.assertIn("decision=accepted", lines[0])
        self.assertIn("subjects=[evt.gitea.notify.h1.bob]", lines[0])
        self.assertIn("actor=lisi", lines[0])

    def test_unsubscribed_subject_is_not_delivered(self):
        """负向：换成别的订阅就收不到 —— 说明上面的成功不是「订阅了就能收一切」。"""
        other = MiniNatsClient(self.url)
        try:
            other.subscribe("totally.unrelated.>")
            self.post(_payload("acme/w2n", "wangwu", 8, "@alice x"), "neg-1")
            self.assertEqual(other.drain(1.0), [],
                             "不相关的订阅也收到了消息")
            self.assertTrue(self.sub.drain(1.0), "但正确的订阅者应该收到")
        finally:
            other.close()

    def test_reconnect_does_not_lose_the_delivery_semantics(self):
        """relay 配的是 max_reconnect_attempts=-1（无限重连），所以断线后
        它会自己接回来 —— 这本身是对的（不能因为一次抖动就停止服务）。

        代价是**不能**用「关掉连接」来模拟发布失败：会立刻重连成功，
        publish 照常成功，测出来的永远是 200。真正的失败必须让 NATS
        彻底不可达 —— 见 PublishFailureTest。
        """
        self.sub.drain(0.3)
        status, _ = self.post(
            _payload("acme/w2n", "still-here", 55, "@alice 还在"), "recon-1")
        self.assertEqual(status, 200)
        self.assertTrue(self.sub.drain(2.0),
                        "NATS 正常时消息必须能到达")


@unittest.skipUnless(shutil.which("docker") or os.environ.get("W2N_NATS"),
                     "需要 docker 或 W2N_NATS")
class PublishFailureTest(unittest.TestCase):
    """NATS **彻底不可达**时，relay 必须诚实报错，不能假装成功。

    单独一个类而不是塞进 RealNatsEndToEndTest，因为它测的是「连不上」
    这条路径，且不该影响共享的 NATS 容器（曾经在这里 tearDown 掉容器，
    把后面的 RealNatsEndToEndTest 连累成 skip）。

    这一组需要真 NATS（要中途把它停掉来模拟「发布时服务不可达」），
    所以加 skipUnless；它用**自己那份**容器，不会影响共享的那份。
    """

    @classmethod
    def setUpClass(cls):
        cls.url = _nats_or_skip(NATS_PF)
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        # 每个用例都从「NATS 活着」开始：上一条会把容器停掉，而用例
        # 之间是有顺序的（字母序），下一个不能假设它还在。
        NATS_PF().start()

    def tearDown(self):
        try:
            NATS_PF().stop()
        except RuntimeError:
            pass

    def _relay_against(self, url):
        cfg = config_mod.load(path=None, env={
            "NATS_SERVERS": url, "NATS_USER": "e2e", "NATS_PASSWORD": "e2e",
            "WEBHOOK_SECRET": SECRET, "PUBLISH_TIMEOUT_SECONDS": "2"})
        relay = _RealRelay(cfg, core.Members(MEMBERS))
        relay.start()
        app = server_mod.App(cfg, providers_mod.build_registry(cfg),
                             core.Members(MEMBERS),
                             core.Seen(os.path.join(self.tmp, "seen.json"), 100),
                             relay)
        httpd = server_mod.serve(app, "127.0.0.1:0")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)
        return relay, httpd, httpd.server_address[1]

    def _post(self, port, delivery):
        body = json.dumps(_payload("acme/w2n", "li", 1, "@alice hi"),
                          ensure_ascii=False).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            s.sendall((f"POST /gitea-hook HTTP/1.1\r\nHost: x\r\n"
                       f"Content-Length: {len(body)}\r\n"
                       f"X-Gitea-Delivery: {delivery}\r\n"
                       f"X-Gitea-Signature: {sig}\r\n"
                       f"X-Gitea-Event: issue_comment\r\n\r\n").encode() + body)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            n = int(re.search(rb"Content-Length:\s*(\d+)", head, re.I).group(1))
            while len(rest) < n:
                rest += s.recv(65536)
            return int(head.split(b" ")[1]), json.loads(rest[:n])
        finally:
            s.close()

    def test_message_survives_a_nats_outage(self):
        """NATS 停机期间投递，**重连后消息真的补发**。

        实测结论（第一版断言写反了，就是靠这条查出来的）：
        停机期间 relay 仍然回 `200 accepted`，而且**这是诚实的** ——
        nats-py 把消息缓存在重连队列里，服务恢复后会补发，订阅端确实收到。

        这条必须测，因为它是整个「无限重连」设计的关键性质：平台在
        NATS 抖动期间收到 200 之后**不会**重试，所以如果消息在抖动中
        丢了，那就永远丢了。

        风险在别处（见下一条）：relay 进程在断连期间重启，缓冲区就没了。
        """
        relay = httpd = None
        server = NATS_PF()
        try:
            relay, httpd, port = self._relay_against(self.url)

            server.stop()                      # ← NATS 彻底没了
            status, doc = self._post(port, "outage-1")
            self.assertEqual(status, 200,
                             f"停机期间应仍然 accepted（消息进重连队列），"
                             f"实际 {status} {doc}")
            self.assertEqual(doc["status"], "accepted")
            self.assertEqual(doc["subjects"], ["evt.gitea.notify.h1.alice"])

            server.start()                     # ← NATS 回来了
            sub = MiniNatsClient(self.url)
            try:
                sub.subscribe("evt.>")
                got = sub.drain(5.0)
            finally:
                sub.close()
            self.assertTrue(got,
                            "平台已经收到 200，不会再重试 —— "
                            "如果消息没补发，它就永久丢了")
            subject, payload = got[0]
            self.assertEqual(subject, "evt.gitea.notify.h1.alice")
            self.assertEqual(json.loads(payload)["delivery"], "outage-1")
        finally:
            if httpd:
                httpd.shutdown()
                httpd.server_close()
            if relay:
                relay.stop()
            try:
                server.stop()
            except RuntimeError:
                pass

    def test_outage_does_not_get_reported_as_failure(self):
        """反过来也成立：停机**不该**报 502。

        因为消息确实会补发，报失败会让平台重试 → 补发 + 重投 = 重复门铃。
        这个平衡点就是「无限重连 + 缓冲」的代价：宁可重复也不丢。
        """
        relay = httpd = None
        server = NATS_PF()
        try:
            relay, httpd, port = self._relay_against(self.url)
            server.stop()
            status, doc = self._post(port, "outage-2")
            self.assertEqual(status, 200,
                             f"报失败会导致重投 + 补发 = 重复门铃：{doc}")
        finally:
            if httpd:
                httpd.shutdown()
                httpd.server_close()
            if relay:
                relay.stop()
            try:
                server.stop()
            except RuntimeError:
                pass

    def test_delivery_is_marked_done_so_platform_will_not_retry(self):
        """accepted 之后 delivery 必须标记完成 —— 否则平台重试会重复门铃。

        这条和上一条是同一个权衡的两面：宁可重复也不丢，所以缓冲期内
        必须告诉平台「成了，别重试」，并自己保证补发。
        """
        relay = httpd = None
        server = NATS_PF()
        try:
            relay, httpd, port = self._relay_against(self.url)
            server.stop()
            self._post(port, "outage-3")
            app = httpd.RequestHandlerClass.app
            self.assertIn(app.seen.begin("outage-3"), ("reserved", "seen"),
                          "accepted 之后 delivery 必须被标记完成")
        finally:
            if httpd:
                httpd.shutdown()
                httpd.server_close()
            if relay:
                relay.stop()
            try:
                server.stop()
            except RuntimeError:
                pass


    def test_startup_refuses_to_run_against_a_dead_nats(self):
        """连都连不上时，relay 必须**拒绝启动**，而不是起来之后每条都失败。

        这条更值钱：服务在配置错误的状态下运行，故障要等第一条 webhook
        才暴露，而那时可能已经过了很久。
        """
        cfg = config_mod.load(path=None, env={
            "NATS_SERVERS": "nats://127.0.0.1:1", "NATS_USER": "e2e",
            "NATS_PASSWORD": "e2e", "WEBHOOK_SECRET": SECRET})
        import app as app_mod
        r = app_mod.Relay(cfg, core.Members(MEMBERS))
        import asyncio

        async def go():
            with self.assertRaises(Exception):
                await asyncio.wait_for(r.connect(), timeout=20)

        asyncio.run(go())


class FieldBlacklistTest(unittest.TestCase):
    """黑名单的匹配规则 —— 这两条规则是刻意分开的，合并不了。

    密钥类按**子串**判：防的是「将来有人加了 `_mark(webhook_secret=...)`」。
    正文类按**精确**判：按子串会误伤 `comment_url`，而那是刻意要记的字段。
    """

    SECRET_NAMES = ["secret", "webhook_secret", "SECRET", "password",
                    "db_password", "authorization", "authorization_header",
                    "token", "api_key", "signature", "credential"]

    BODY_NAMES = ["body", "comment_body", "review_content", "payload",
                  "raw_body", "raw_payload"]

    LEGIT_NAMES = ["comment_url", "url", "title", "actor", "repo", "mentions",
                   "subjects", "delivery", "number", "payload_size", "kind"]

    def test_secret_variants_are_blocked_in_both_modes(self):
        for name in self.SECRET_NAMES:
            with self.subTest(name=name):
                self.assertEqual(logs_mod.safe_fields({name: "LEAK"}), {},
                                 f"{name} 在默认模式下没被拦")
                self.assertEqual(
                    logs_mod.safe_fields({name: "LEAK"}, allow_bodies=True),
                    {}, f"{name} 在逃生开关打开时仍没被拦 —— 密钥没有开关")

    def test_body_blocked_by_default_released_by_switch(self):
        for name in self.BODY_NAMES:
            with self.subTest(name=name):
                self.assertEqual(logs_mod.safe_fields({name: "x"}), {},
                                 f"{name} 默认没被拦")
                self.assertEqual(
                    logs_mod.safe_fields({name: "x"}, allow_bodies=True),
                    {name: "x"}, f"逃生开关对 {name} 没生效")

    def test_legit_fields_are_not_collateral_damage(self):
        """`comment_url` 之类必须放行 —— 它们已经发给 Agent 了。"""
        for name in self.LEGIT_NAMES:
            with self.subTest(name=name):
                self.assertEqual(
                    logs_mod.safe_fields({name: "v"}, allow_bodies=True),
                    {name: "v"}, f"{name} 被误伤了")

    def test_allow_bodies_argument_does_not_mutate_global_state(self):
        """safe_fields 必须是纯的。

        第一版把 `allow_bodies` 写成了会改全局，于是「先调一次
        allow_bodies=True」之后整个进程都开始放行正文 —— 而这个决定
        本该由配置一次定下。测试很容易漏，因为调用顺序不对时
        结果看起来完全正常。
        """
        old = logs_mod._ALLOW_BODIES
        try:
            logs_mod._ALLOW_BODIES = False
            logs_mod.safe_fields({"body": "x"}, allow_bodies=True)
            self.assertFalse(logs_mod._ALLOW_BODIES,
                             "传参修改了进程级开关")
            self.assertEqual(logs_mod.safe_fields({"body": "x"}), {},
                             "全局被污染后，默认模式下正文没被拦")
        finally:
            logs_mod._ALLOW_BODIES = old

    def test_no_active_exception_does_not_log_none_type_noise(self):
        """exc_info=True 但没有活动异常时，别写出 "NoneType: None"。"""
        stream = io.StringIO()
        logs_mod.setup(config_mod.LogConfig(), stream=stream)
        try:
            logs_mod.event(logs_mod.get_logger(), logging.ERROR, "x",
                           exc_info=True)
        finally:
            logs_mod.setup(config_mod.LogConfig())
        self.assertNotIn("NoneType", stream.getvalue(),
                         "没有异常时不该写出 NoneType: None 噪音")
        self.assertNotIn("exc=", stream.getvalue())


class _RealRelay:
    """把 app.Relay 跑在后台事件循环里，供进程内测试使用。

    必须代理 submit_event / subjects_for —— server 只认这两个名字，
    不代理的话测试会看到一个「连上了 NATS 却发不出东西」的假象，
    症状与真正的 bug 一模一样。
    """

    def __init__(self, cfg, members):
        import app as app_mod
        self.relay = app_mod.Relay(cfg, members)
        self._loop = None
        self._thread = None

    # ── 代理给真正的 Relay ──
    def submit_event(self, ev):
        return self.relay.submit_event(ev)

    def subjects_for(self, ev):
        return self.relay.subjects_for(ev)

    @property
    def nc(self):
        return self.relay.nc

    # ── 生命周期 ──
    def start(self):
        import asyncio

        ready = threading.Event()
        failed = []

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self.relay.connect())
            except Exception as e:      # noqa
                failed.append(e)
                ready.set()
                return
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not ready.wait(15):
            raise RuntimeError("relay 连 NATS 超时")
        if failed:
            raise RuntimeError(f"relay 连不上 NATS: {failed[0]!r}")

    def stop(self):
        if self._loop:
            self.relay._shutdown = True
            self._loop.call_soon_threadsafe(self._loop.stop)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2, exit=False)
    finally:
        NatsServer.cleanup()