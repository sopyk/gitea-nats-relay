"""与平台、与 HTTP 都无关的核心：delivery 去重、成员映射、收件人解析、公告判定。

单独抽出来是为了让这部分能被直接测 —— 原来它们挤在 server.py 里，
测试只能整个替换掉 NATS 客户端才能跑，等于没测。
"""
import json
import os
import threading
from collections import OrderedDict


class Seen:
    """delivery UUID 去重：内存 LRU + 落盘，relay 重启不重复叫醒。

    三态：reserved（占位成功，处理完必须 commit 或 rollback）/ seen / inflight。
    **只有全部 NATS 发布成功才 commit** —— 反过来做的话，一次网络抖动就会让
    下游永远收不到这条通知，而且平台已被告知「投递成功」不会再重试。
    """

    def __init__(self, path, cap=2000):
        self.path = path
        self.cap = max(1, cap)
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
        while len(self.od) > self.cap:
            self.od.popitem(last=False)

    def _persist_locked(self):
        # 先写临时文件再 os.replace：保证读到的永远是完整的一份。
        # 注意这里没有 fsync —— 掉电场景下最后一次 commit 可能丢，
        # 后果是最多一次重复通知（at-most-once 语义可接受），不是漏通知。
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(self.od.keys()), f)
        os.replace(tmp, self.path)

    def begin(self, key):
        """返回 seen / inflight / reserved。"""
        if not key:
            # 没有 delivery 头就无法去重。调用方应当拒绝而不是放行，
            # 否则同一个事件每次重投都会重复叫醒 —— 见 handler。
            return "missing"
        with self.lock:
            if key in self.od:
                return "seen"
            if key in self.inflight:
                return "inflight"
            self.inflight.add(key)
            return "reserved"

    def commit(self, key):
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
        """发布失败/超时：释放占位，允许来源平台用同一 delivery 重试。"""
        if not key:
            return
        with self.lock:
            self.inflight.discard(key)


class Members:
    """登录名 → {host, id}。加载时逐条校验（见 config.load_members）。"""

    def __init__(self, mapping=None):
        self._m = mapping or {}

    def resolve(self, login):
        return self._m.get(login)

    def __len__(self):
        return len(self._m)


def extract_targets(ev, members):
    """收件人 = assignee ∪ 被 @ 的人 ∪ 评审人，取各自映射后的收件位。

    同一个 (host, id) 只发一次 —— 既是 assignee 又是评审人时不该收到两条门铃。
    """
    logins = set()
    logins.update(ev.get("assignees") or [])
    logins.update(ev.get("mentions") or [])
    logins.update(ev.get("reviewers") or [])
    seen, targets = set(), []
    for lg in logins:
        m = members.resolve(lg)
        if not m:
            continue
        key = (m["host"], m["id"])
        if key in seen:
            continue
        seen.add(key)
        targets.append(key)
    return targets


def is_broadcast(ev, broadcast_authors):
    """公告判定：仅配置作者新建/重开、且带公告标签或【公告】标题的 issue。"""
    if ev.get("kind") != "issue" or ev.get("action") not in ("opened", "reopened"):
        return False
    if ev.get("actor") not in broadcast_authors:
        return False
    labels = ev.get("labels") or []
    title = ev.get("title") or ""
    return any("公告" in str(x) for x in labels) or title.startswith("【公告】")