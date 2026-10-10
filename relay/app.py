#!/usr/bin/env python3
"""Webhook2NATS — Webhook → Core NATS 实时门铃转发（无 JetStream）

装配：加载配置 → 建 provider 注册表 → 读成员映射 → 连 NATS → 起 HTTP。

职责边界见 README。本文件只做装配，不含业务逻辑。
"""
import asyncio
import os
import signal
import sys

import nats

import config as config_mod
import core
import providers as providers_mod
import server as server_mod


def log(*a):
    print("[relay]", *a, flush=True)


class Relay:
    """NATS 连接与发布。"""

    def __init__(self, cfg, members):
        self.cfg = cfg
        self.members = members
        self.nc = None
        self.loop = None
        self._shutdown = False

    async def connect(self):
        self.nc = await nats.connect(
            servers=self.cfg.nats_servers,
            user=self.cfg.nats_user,
            password=self.cfg.nats_password,
            connect_timeout=10,
            max_reconnect_attempts=-1,
            name=self.cfg.client_name,
        )
        log("NATS connected:", self.cfg.nats_servers)
        self.loop = asyncio.get_running_loop()
        if self.cfg.relay_id:
            asyncio.create_task(self._heartbeat())
        else:
            log("RELAY_ID not set; heartbeat disabled")

    async def _heartbeat(self):
        subject = f"hb.{self.cfg.relay_id}"
        while not self._shutdown:
            try:
                await self.nc.publish(subject,
                                       f"hb|{self.cfg.relay_id}|online".encode())
                await self.nc.flush()
            except Exception as e:  # noqa
                log("heartbeat error:", e)
            await asyncio.sleep(self.cfg.heartbeat_seconds)

    async def _publish(self, subject, obj):
        import json
        data = json.dumps(obj, ensure_ascii=False).encode()
        await self.nc.publish(subject, data)
        # 把超时传进去：旧代码不传，用的是 nats-py 的默认 10s，
        # 而 HTTP 层 3s 就返回 504 了 —— 协程仍会继续跑完并可能投递成功，
        # 平台再重试一次就成了重复门铃。
        await self.nc.flush(timeout=self.cfg.publish_timeout_seconds)

    def subjects_for(self, ev):
        subjects = []
        if core.is_broadcast(ev, self.cfg.broadcast_authors):
            subjects.append(self.cfg.broadcast_subject)
        for host, aid in core.extract_targets(ev, self.members):
            subjects.append(
                f"{self.cfg.notify_subject_prefix}.{host}.{aid}")
        return subjects

    def submit_event(self, ev):
        """从 HTTP 线程调用；实际发布在 NATS 事件循环里跑。"""
        async def run():
            results = []
            for subject in self.subjects_for(ev):
                try:
                    await self._publish(subject, ev)
                    results.append((subject, True))
                except Exception as e:  # noqa
                    log("publish failed:", subject, e)
                    results.append((subject, False))
            return results
        return asyncio.run_coroutine_threadsafe(run(), self.loop)

    def shutdown(self):
        self._shutdown = True


async def amain():
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        log("ERROR:", e)
        sys.exit(1)
    for w in getattr(cfg, "warnings", []):
        log("WARN:", w)
    if not cfg.broadcast_authors:
        log("WARN: broadcast_authors 未配置，全厅公告已禁用")

    try:
        members_raw = config_mod.load_members(cfg.members_file)
    except config_mod.ConfigError as e:
        log("ERROR:", e)
        sys.exit(1)
    members = core.Members(members_raw)
    log("members loaded:", len(members))

    seen = core.Seen(cfg.dedup_file, cfg.dedup_capacity)
    try:
        registry = providers_mod.build_registry(cfg)
    except ValueError as e:
        log("ERROR:", e)
        sys.exit(1)

    relay = Relay(cfg, members)
    await relay.connect()

    app = server_mod.App(cfg, registry, members, seen, relay)
    httpd = server_mod.serve(app, cfg.listen)
    log("listening on", cfg.listen, "providers:", list(registry))

    stop = asyncio.Event()

    def _on_signal():
        log("shutdown signal received")
        relay.shutdown()
        stop.set()
        # 放在事件循环线程之外，避免阻塞正在跑的 publish 协程
        threading_shutdown()

    import threading
    def threading_shutdown():
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    await asyncio.get_running_loop().run_in_executor(None, httpd.serve_forever)
    await stop.wait()
    log("server stopped")


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(0)