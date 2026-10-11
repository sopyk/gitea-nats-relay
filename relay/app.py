#!/usr/bin/env python3
"""Webhook2NATS — Webhook → Core NATS 实时门铃转发（无 JetStream）

装配：加载配置 → 建 provider 注册表 → 读成员映射 → 连 NATS → 起 HTTP。

职责边界见 README。本文件只做装配，不含业务逻辑。
"""
import asyncio
import json
import logging
import os
import signal
import sys
import threading

import nats

import config as config_mod
import core
import logs as logs_mod
import providers as providers_mod
import server as server_mod

logger = logs_mod.get_logger()


def log(message, **fields):
    """结构化日志的简写。字段一律走 extra，不拼进 message。

    保留这个名字是为了让既有调用点不必大改 —— 但新写的代码直接用
    logs_mod.event()，级别更明确。
    """
    logs_mod.event(logger, logging.INFO, message, **fields)


def warn(message, **fields):
    logs_mod.event(logger, logging.WARNING, message, **fields)


def error(message, **fields):
    logs_mod.event(logger, logging.ERROR, message, **fields)


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
        log("NATS connected", servers=self.cfg.nats_servers)
        self.loop = asyncio.get_running_loop()
        if self.cfg.relay_id:
            asyncio.create_task(self._heartbeat())
        else:
            warn("RELAY_ID 未设置，心跳已禁用")

    async def _heartbeat(self):
        subject = f"hb.{self.cfg.relay_id}"
        while not self._shutdown:
            try:
                await self.nc.publish(subject,
                                       f"hb|{self.cfg.relay_id}|online".encode())
                await self.nc.flush()
            except Exception as e:  # noqa
                # 心跳是后台任务，异常不能让它静默死掉 —— 否则 relay_id
                # 会一直显示在线，实际却早就不发心跳了。
                warn("心跳发送失败", relay_id=self.cfg.relay_id, error=repr(e))
            await asyncio.sleep(self.cfg.heartbeat_seconds)

    def _nats_timeout(self):
        """NATS 侧预算必须**小于** HTTP 层预算。

        publish_timeout_seconds 是 HTTP 层的等待上限。如果两处用同一个值，
        HTTP 的计时从 submit_event 起算，而 flush 的计时要等 publish 返回之后
        才开始 —— 于是 HTTP 必然先超时：它回了 504、delivery 回滚、平台重试，
        而协程还在等 PONG，稍后 flush 成功，消息**其实早已上线** → 重复门铃。

        cancel() 救不了这个：它中止的是「等 PONG」，撤不回已经写进 socket 的字节。
        唯一能缩小这个窗口的办法是让 NATS 先于 HTTP 放弃。
        """
        return max(0.1, self.cfg.publish_timeout_seconds * 0.8)

    async def _publish(self, subject, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        await self.nc.publish(subject, data)
        # 传 timeout 而不是用 nats-py 的默认 10s —— 见 _nats_timeout 的说明
        await self.nc.flush(timeout=self._nats_timeout())

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
                    error("发布失败", subject=subject, error=repr(e))
                    results.append((subject, False))
            return results
        return asyncio.run_coroutine_threadsafe(run(), self.loop)

    def shutdown(self):
        self._shutdown = True

    async def aclose(self):
        """优雅关闭 NATS 连接。

        只设 _shutdown 的话，NATS 那边看到的是硬断连：正在飞的 publish 被丢弃，
        服务端的订阅者连接记录也来不及清理。drain() 会把缓冲里剩下的消息
        发完再关 —— 这是 Core NATS 下唯一能保证「已确认的发布确实到达」的办法。
        """
        self._shutdown = True
        if self.nc is None:
            return
        try:
            await asyncio.wait_for(self.nc.drain(), timeout=5)
            log("NATS 已排空")
        except Exception as e:  # noqa
            error("排空失败，仍将关闭", error=repr(e))
            try:
                await self.nc.close()
            except Exception:  # noqa
                pass


async def amain():
    # 配置还没加载，所以这里只有 stdout handler —— 那正是我们要的：
    # 「配置错了」这件事本身必须先打出来，才谈得上按配置写日志文件。
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        error("配置不合法，启动中止", detail=str(e))
        sys.exit(1)
    except Exception as e:  # noqa
        # 配置层的 bug（比如某个字段类型没覆盖到）会以 ValueError/TypeError 冒出来。
        # 那时至少要打印得比裸 traceback 清楚 —— 运维只关心「哪项配置不对」。
        error("加载配置时发生未预期的异常", exc=type(e).__name__, detail=str(e))
        error("请检查配置文件与 config.py 的类型覆盖是否一致")
        sys.exit(1)

    # 从这里开始才有真正的日志配置。
    degraded = logs_mod.setup(cfg.logging)
    if degraded:
        warn(degraded)

    for w in getattr(cfg, "warnings", []):
        warn(w)
    if not cfg.broadcast_authors:
        warn("broadcast_authors 未配置，全厅公告已禁用")

    try:
        members_raw = config_mod.load_members(cfg.members_file)
    except config_mod.ConfigError as e:
        log("ERROR:", e)
        sys.exit(1)
    members = core.Members(members_raw)
    log("成员表已加载", count=len(members), file=cfg.members_file)

    seen = core.Seen(cfg.dedup_file, cfg.dedup_capacity)
    try:
        registry = providers_mod.build_registry(cfg)
    except ValueError as e:
        error("provider 注册失败，启动中止", detail=str(e))
        sys.exit(1)

    relay = Relay(cfg, members)
    await relay.connect()

    app = server_mod.App(cfg, registry, members, seen, relay)
    httpd = server_mod.serve(app, cfg.listen)
    log("开始监听", listen=cfg.listen, providers=list(registry))

    stop = asyncio.Event()

    def _on_signal():
        """信号处理器跑在事件循环线程里，所以不能阻塞地关 HTTP server。"""
        warn("收到关停信号，开始优雅关闭")
        relay.shutdown()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    await asyncio.get_running_loop().run_in_executor(None, httpd.serve_forever)
    await stop.wait()

    # httpd.shutdown() 之后可能有 handler 线程还在收尾，等它们退出再关 NATS，
    # 否则 drain 完连接又冒出新的发布。
    httpd.server_close()
    await relay.aclose()
    log("服务已停止")


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        # Ctrl-C 走信号处理器已经关过一次了，这里只是别让 Python 再打一段
        # 难看的 traceback 盖住真正的关停日志。
        sys.exit(0)