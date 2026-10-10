#!/usr/bin/env python3
"""
gitea-nats-relay — Gitea webhook -> Core NATS 实时事件 push（阶段2，无 JetStream）

职责边界：
- 只做"门铃"：收 Gitea webhook，校验 HMAC，去重，解析被点名/指派成员，
  发布精简事件到 NATS，把相关 Agent 秒级叫醒。
- 不持久化消息、不标已读、不替任何 Agent 处理；离线漏事件由低频 A7 巡厅
  以 Gitea 未读为唯一对账事实源兜底。
- 正式留痕仍在 Gitea；NATS 上不保存任何正文历史。

Subject 契约（服务端 ACL 锁定，默认前缀，均可用环境变量覆盖）：
- <NOTIFY_SUBJECT_PREFIX>.<host>.<id>   定向通知（默认 evt.gitea.notify，仅 relay 可发，本人可订）
- <BROADCAST_SUBJECT>                    公告全厅通知（默认 evt.gitea.broadcast，仅 relay 可发，全员可订）
- hb.<RELAY_ID>                          本服务心跳（RELAY_ID 由配置提供）

配置（环境变量）。所有环境相关值（主机、域名、账号、IP）一律从环境/挂载配置注入，
代码与镜像中不内置任何具体环境值：
  NATS_SERVERS         例 nats://<nats-host>:4222（逗号分隔多个）
  NATS_USER / NATS_PASSWORD   gitea-relay 专用账号
  RELAY_ID             心跳标识后缀，如 <host>.gitea-relay；留空则不发心跳
  RELAY_CLIENT_NAME    NATS 连接显示名，默认 gitea-nats-relay
  NATS_HEARTBEAT_SECONDS       默认 60
  WEBHOOK_SECRET       Gitea webhook 配置里同一个密钥（HMAC-SHA256）
  MEMBERS_FILE         登录名 -> {host,id} 映射 JSON，默认 /config/members.json
  DEDUP_FILE           去重持久化文件，默认 /data/seen.json
  LISTEN               默认 0.0.0.0:8090
  BROADCAST_AUTHORS    必填：逗号分隔登录名，这些作者新建带"公告"标签/标题的 issue 走全厅
  NOTIFY_SUBJECT_PREFIX 默认 evt.gitea.notify
  BROADCAST_SUBJECT    默认 evt.gitea.broadcast
"""
import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import sys
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import nats

# Gitea 登录名规则（本厅登录名不含点号，点号是 NATS subject 分隔符）
_MENTION_RE = re.compile(r"(?<![\w.@/])@([A-Za-z0-9_-]+)(?![\w.])")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def extract_mentions(text):
    """剥离代码块/行内码后抽取 @登录名（代码里的 @ 不触发通知）。"""
    if not text:
        return []
    text = _CODE_FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    return _MENTION_RE.findall(text)


def log(*a):
    print("[relay]", *a, flush=True)


MEMBERS = {}
# 公告作者必须显式配置（登录名逗号分隔），代码里不内置任何具体账号。
BROADCAST_AUTHORS = set(
    x for x in os.environ.get("BROADCAST_AUTHORS", "").split(",") if x.strip()
)
# subject 前缀是协议契约，给默认值但允许环境覆盖（多实例/多站点时隔离）。
NOTIFY_SUBJECT_PREFIX = os.environ.get("NOTIFY_SUBJECT_PREFIX", "evt.gitea.notify")
BROADCAST_SUBJECT = os.environ.get("BROADCAST_SUBJECT", "evt.gitea.broadcast")


def load_members():
    path = os.environ.get("MEMBERS_FILE", "/data/members.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # 兼容 {"members": {login: {host,id}}} 或平铺 {login:{host,id}}
        MEMBERS.clear()
        MEMBERS.update(data.get("members", data))
        log("members loaded:", len(MEMBERS))
    except FileNotFoundError:
        log("WARN members file not found:", path, "(only broadcast will work)")


class Seen:
    """delivery UUID 去重：内存 LRU + 落盘，relay 重启不重复叫醒。"""

    def __init__(self, path, cap=2000):
        self.path = path
        self.cap = cap
        self.od = OrderedDict()
        self.inflight = set()
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                for k in json.load(f):
                    self.od[k] = True
        except (FileNotFoundError, ValueError):
            pass

    def _persist_locked(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(self.od.keys()), f)
        os.replace(tmp, self.path)

    def begin(self, key):
        """开始处理一个 delivery。

        返回：
        - seen: 此前已成功处理，直接 duplicate
        - inflight: 同 delivery 正在处理（Gitea 重叠重试），调用方返回 409
        - reserved: 本次占位成功，处理完必须 commit 或 rollback
        """
        if not key:
            return "reserved"
        with self.lock:
            if key in self.od:
                return "seen"
            if key in self.inflight:
                return "inflight"
            self.inflight.add(key)
            return "reserved"

    def commit(self, key):
        """仅在所有 NATS 发布成功后确认 delivery，之后重复投递才丢弃。"""
        if not key:
            return
        with self.lock:
            self.inflight.discard(key)
            self.od[key] = True
            self.od.move_to_end(key)
            while len(self.od) > self.cap:
                self.od.popitem(last=False)
            self._persist_locked()

    def rollback(self, key):
        """发布失败/超时：释放占位，允许 Gitea 用同一 delivery 重试。"""
        if not key:
            return
        with self.lock:
            self.inflight.discard(key)


SEEN = None  # type: ignore


def base_url(payload):
    repo = (payload.get("repository") or {})
    raw = repo.get("html_url") or ""
    # html_url 形如 http://host:port/songsir-group/AgentCouncil
    return raw.rsplit("/", 2)[0] if raw.count("/") >= 2 else ""


def compact(payload, kind, action):
    """压成 ≤~1KB 的门铃：足够判断相关性，正文让 agent 拿 API 拉。"""
    repo = payload.get("repository") or {}
    actor = (payload.get("sender") or {}).get("login", "")
    out = {
        "kind": kind,            # issue | issue_comment | pull_request | review
        "action": action,
        "repo": repo.get("full_name", ""),
        "actor": actor,
        "ts": payload.get("action_at") or "",
    }
    obj = payload.get("issue") or payload.get("pull_request") or {}
    if obj:
        out["number"] = obj.get("number")
        out["title"] = (obj.get("title") or "")[:140]
        out["url"] = (obj.get("html_url") or "")
        out["state"] = obj.get("state")
        out["assignees"] = [a.get("login") for a in (obj.get("assignees") or []) if a]
        if obj.get("pull_request") is not None or "pull_request" in kind:
            out["is_pr"] = True
        labels = obj.get("labels") or []
        out["labels"] = [l.get("name") for l in labels if isinstance(l, dict)]
    c = payload.get("comment") or {}
    if c:
        out["comment_url"] = c.get("html_url", "")
        api_mentions = [m.get("login") for m in (c.get("mentions") or []) if m]
        out["mentions"] = sorted(set(api_mentions) | set(extract_mentions(c.get("body") or "")))
    rev = payload.get("review") or {}
    if rev:
        out["review_state"] = rev.get("state")
        api_mentions = [m.get("login") for m in (rev.get("mentions") or []) if m]
        out["mentions"] = sorted(set(api_mentions) | set(extract_mentions(rev.get("body") or "")))
    reviewers = obj.get("requested_reviewers") or []
    if reviewers:
        out["reviewers"] = [r.get("login") for r in reviewers if isinstance(r, dict)]
    return out


def extract_targets(ev):
    logins = set()
    logins.update(ev.get("assignees") or [])
    logins.update(ev.get("mentions") or [])
    logins.update(ev.get("reviewers") or [])
    # 被请求评审（Gitea 放在 pull_request requested_reviewers 已在 assignees? 兜底从原始无需）
    targets = []
    for lg in logins:
        m = MEMBERS.get(lg)
        if m and m.get("host") and m.get("id"):
            targets.append((m["host"], m["id"]))
    return targets


def is_broadcast(ev):
    if ev.get("kind") != "issue" or ev.get("action") not in ("opened", "reopened"):
        return False
    if ev.get("actor") not in BROADCAST_AUTHORS:
        return False
    labels = ev.get("labels") or []
    title = ev.get("title") or ""
    return any("公告" in str(x) for x in labels) or title.startswith("【公告】")


class Relay:
    def __init__(self):
        self.nc = None
        self.loop = None
        # 心跳标识（含主机标识）必须由环境提供，代码里不内置具体主机号/服务名。
        self.relay_id = os.environ.get("RELAY_ID", "")
        self.client_name = os.environ.get("RELAY_CLIENT_NAME", "gitea-nats-relay")

    async def connect(self):
        servers = os.environ["NATS_SERVERS"]
        self.nc = await nats.connect(
            servers=servers.split(","),
            user=os.environ.get("NATS_USER"),
            password=os.environ.get("NATS_PASSWORD"),
            connect_timeout=10,
            max_reconnect_attempts=-1,
            name=self.client_name,
        )
        log("NATS connected:", servers)
        self.loop = asyncio.get_running_loop()
        if self.relay_id:
            asyncio.create_task(self._heartbeat())
        else:
            log("RELAY_ID not set; heartbeat disabled")

    async def _heartbeat(self):
        period = int(os.environ.get("NATS_HEARTBEAT_SECONDS", "60"))
        while True:
            try:
                await self.nc.publish(f"hb.{self.relay_id}",
                                      f"hb|{self.relay_id}|online".encode())
            except Exception as e:  # noqa
                log("heartbeat error:", e)
            await asyncio.sleep(period)

    async def _publish(self, subject, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        await self.nc.publish(subject, data)
        await self.nc.flush()
        log("->", subject, obj.get("number"), obj.get("action"))

    def submit_event(self, ev):
        async def run():
            subjects = []
            if is_broadcast(ev):
                subjects.append(BROADCAST_SUBJECT)
            for host, aid in extract_targets(ev):
                subjects.append(f"{NOTIFY_SUBJECT_PREFIX}.{host}.{aid}")
            results = []
            for subject in subjects:
                try:
                    await self._publish(subject, ev)
                    results.append((subject, True))
                except Exception as e:
                    log("publish failed:", subject, e)
                    results.append((subject, False))
            return results
        future = asyncio.run_coroutine_threadsafe(run(), self.loop)
        return future


RELAY = Relay()


def verify_signature(secret, body, sig):
    if not sig:
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, sig.strip())


EVENT_MAP = {
    "issues": "issue",
    "issue_comment": "issue_comment",
    "pull_request": "pull_request",
    "pull_request_review": "review",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "gitea-nats-relay/1.0"

    def _reply(self, code, msg="ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(msg.encode())

    def log_message(self, *a):  # 静默默认 access log
        pass

    def do_GET(self):
        if self.path == "/healthz":
            ok = RELAY.nc is not None and RELAY.nc.is_connected
            self._reply(200 if ok else 503, "ok" if ok else "nats-down")
        else:
            self._reply(404, "not-found")

    def do_POST(self):
        if self.path != "/gitea-hook":
            return self._reply(404, "not-found")
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        secret = os.environ.get("WEBHOOK_SECRET", "")
        sig = self.headers.get("X-Gitea-Signature", "")
        if not secret or not verify_signature(secret, body, sig):
            log("rejected: bad signature from", self.client_address[0])
            return self._reply(401, "bad-signature")
        delivery = self.headers.get("X-Gitea-Delivery", "")
        gitea_event = self.headers.get("X-Gitea-Event", "")
        dedup_state = SEEN.begin(delivery)
        if dedup_state == "seen":
            return self._reply(200, "duplicate")
        if dedup_state == "inflight":
            return self._reply(409, "inflight")
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:  # noqa
            SEEN.rollback(delivery)
            return self._reply(400, "bad-json")
        kind = EVENT_MAP.get(gitea_event)
        if not kind:
            SEEN.commit(delivery)
            return self._reply(200, f"ignored:{gitea_event}")
        action = payload.get("action", "")
        ev = compact(payload, kind, action)
        ev["delivery"] = delivery
        try:
            future = RELAY.submit_event(ev)
            timeout = float(os.environ.get("PUBLISH_TIMEOUT_SECONDS", "3"))
            results = future.result(timeout=timeout)
            if results and not all(ok for _, ok in results):
                failed = ",".join(subject for subject, ok in results if not ok)
                log("webhook accepted but publish failed:", failed)
                SEEN.rollback(delivery)
                return self._reply(502, "publish-failed")
        except concurrent.futures.TimeoutError:
            log("publish timeout for delivery:", delivery)
            SEEN.rollback(delivery)
            return self._reply(504, "publish-timeout")
        except Exception as e:  # noqa
            log("publish exception for delivery:", delivery, e)
            SEEN.rollback(delivery)
            return self._reply(500, "publish-error")
        SEEN.commit(delivery)
        self._reply(200, "accepted")


async def amain():
    global SEEN
    os.makedirs(os.path.dirname(os.environ.get("DEDUP_FILE", "/data/seen.json")),
                exist_ok=True)
    SEEN = Seen(os.environ.get("DEDUP_FILE", "/data/seen.json"))
    load_members()
    await RELAY.connect()
    listen = os.environ.get("LISTEN", "0.0.0.0:8090")
    host, port = listen.split(":")
    httpd = ThreadingHTTPServer((host, int(port)), Handler)
    log("listening on", listen)
    await asyncio.get_running_loop().run_in_executor(None, httpd.serve_forever)


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(0)
