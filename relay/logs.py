"""日志：统一出口，带滚动文件与三重降级保护。

每一道防护都对应一个**实测过**的失败模式，不是假想：

1. **目录不存在 / 不可写** —— `RotatingFileHandler` 在构造时就抛
   `FileNotFoundError`（实测 `/data/logs/relay.log` 直接抛）。先建目录；
   建不出来就**退回纯 stdout + 一条 WARN，绝不退出**。日志是附加能力，
   为了它让一个本来能正常起服务的容器起不来，是本末倒置。

2. **写入时 IO 故障** —— `FileHandler.emit` 自带 try/except，磁盘满 / fd
   关闭都被它吞掉（实测 `OSError: [Errno 9] Bad file descriptor` 后调用方
   继续执行）。所以日志调用不会打断 webhook 处理。

3. **不碰 seen 生命周期** —— 这条不在本文件，在 server.py：日志调用绝不插在
   `seen.begin()` 与 `commit()/rollback()` 之间，否则一旦跳过 rollback，
   那个 delivery 会永久卡在 inflight。

## 隐私规则（硬约束）

> **日志里只允许出现「已经发给 Agent 的字段」。**

`repo / number / actor / ts / title / url / comment_url / mentions` 本来就在
NATS 消息里，Agent 已经持有了，日志再记一遍**不增加任何暴露面**。

真正敏感的只有两个：`comment.body` 和签名头/secret。这两个由 `server.py`
负责不传进来；本模块再兜一层 —— `safe_fields` 会把黑名单字段直接丢掉。
排查正则解析 bug 时可以开 `[logging] log_bodies`，但那是显式的例外开关。
"""
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from datetime import datetime

LOGGER_NAME = "relay"

# 轮转文件的默认上限。5MB × 5 ≈ 25MB，与 compose 里 stdout 的
# 10m × 3 同一量级 —— 两边加起来磁盘占用有界，不会无限长。
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUPS = 5

# 单个字段值的最大长度。payload 里的 title 理论上可以很长，日志不该被
# 一个超长字符串撑爆，也不该因为它被截断而丢掉整行。
MAX_VALUE_CHARS = 200

# log_bodies 逃生开关下 body 字段的单独上限。
#
# 必须比上面大得多：payload 的 JSON 头部（action/repo/sender…）通常就先
# 吃掉前 200 字符，真正要排查的 comment.body 根本轮不到。用普通上限的话
# 这个开关等于白开 —— 实测过，200 字符截断正好切在 issue 对象中间。
#
# 8000 足够看清任何真实场景下的评论正文，又有明确上界。
MAX_BODY_CHARS = 8000

_BODY_FIELD_NAMES = frozenset({"body", "comment_body", "review_content"})

# 永远不进日志的字段名（不区分大小写）。这些**没有开关** ——
# 密钥类的东西没有任何排查场景需要它们，放行等于把事故写进磁盘。
#
# 按**子串**匹配而不是精确匹配：`webhook_secret` / `authorization_header`
# / `db_password` 这些变体同样必须拦住。精确匹配的话，只要将来有人写了
# 一个 `webhook_secret=...` 字段名就绕过了。
_FORBIDDEN_TOKENS = (
    "secret", "password", "passwd", "authorization", "credential",
    "token", "signature", "apikey", "api_key",
)

# 默认也不记，但 `log_bodies = true` 时放行的字段。**精确匹配** ——
# 这些词太通用，按子串拦会误伤我们合法要记的字段。
#
# 放在这里而不是跟密钥类混在一起，是为了让「开关真的有效」这件事
# 只取决于一个地方 —— 之前把 body 放进密钥黑名单当纵深防御，结果逃生
# 开关被自己废了：配置能写、文档写了，实际永远不生效。
_BODY_FIELDS = frozenset({"body", "comment_body", "review_content",
                          "payload", "raw_payload", "raw_body"})

# 由 setup() 设置的逃生开关状态。进程级是合适的 —— 日志配置本来就是
# 进程级的，而且它必须同时影响 event() 和两个 Formatter，两边读同一个
# 值才不会出现「一边放行一边拦住」的不一致。
_ALLOW_BODIES = False

# 不加引号的安全字符集。其余一律加引号并转义 —— subject 里可能有空格
# 之外的怪字符，而日志是要给人 grep 的，歧义比难读更糟。
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_./:@+-]+$")


class LogConfig:
    """[logging] 配置段。

    默认值让**不配置也能正常工作**：纯 stdout、INFO、text 格式、不记正文。
    这很重要 —— 日志是附加能力，不该要求任何人先理解它才能起服务。
    """

    def __init__(self, level="info", log_format="text", file="",
                 max_bytes=DEFAULT_MAX_BYTES, backups=DEFAULT_BACKUPS,
                 log_bodies=False):
        self.level = level
        self.format = log_format
        self.file = file
        self.max_bytes = max_bytes
        self.backups = backups
        self.log_bodies = log_bodies


def _format_value(value, key=None):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value) if isinstance(value, (set, frozenset)) else list(value)
        return "[" + " ".join(_format_value(v, key) for v in items) + "]"
    text = str(value)
    limit = MAX_BODY_CHARS if (key or "").lower() in _BODY_FIELD_NAMES else MAX_VALUE_CHARS
    if len(text) > limit:
        # 标出被截掉了多少，否则读日志的人会以为这就是全部，
        # 基于截断内容下结论（比如「正文里没有 @alice」）是危险的。
        text = text[:limit] + f"…(已截断，共 {len(text)} 字符)"
    return text if _SAFE_TOKEN.match(text) else _quote(text)


def _quote(text):
    """把值包进双引号，并转义掉会破坏「一行一条」的东西。

    只转义反斜杠和引号是不够的：值里一个裸换行就能让一条日志裂成两行，
    第二行可以长得和别的日志一模一样 —— 也就是**伪造日志**。攻击者
    控制得了的字段（delivery 头、error=repr(e)、log_bodies 打开时的
    正文）都能做到，所以这里统一按 C 风格转义所有控制字符。

    ESC（\\x1b）也要转：它能让终端改颜色、清屏、伪造提示符，
    在看 docker logs 的终端上就是实打实的操纵。
    """
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _is_forbidden(name, allow_bodies):
    """字段名是否属于禁止的那一类。

    密钥类按**子串**判：键名是代码写死的、攻击者控不了键只能控值，但
    防的是「将来有人加了 `_mark(webhook_secret=...)`」这种迟早会发生
    的事。按子串能一次覆盖所有变体。

    正文类按**精确**判：body/comment/payload 这类词太通用，按子串会
    误伤 `comment_url` —— 而那是我们**刻意**要记的字段（它跟其它元数据
    一样已经发给 Agent，不增加暴露面）。
    """
    lowered = name.lower()
    if any(tok in lowered for tok in _FORBIDDEN_TOKENS):
        return True
    return not allow_bodies and lowered in _BODY_FIELDS


def safe_fields(fields, allow_bodies=None):
    """剥掉禁止字段。

    **纯函数**：`allow_bodies` 只影响本次调用，绝不改全局。
    （第一版把它写成了会写全局，于是「先调一次 allow_bodies=True」
    之后整个进程都放行了正文 —— 而它本该是配置决定的、一次性的事。
    进程级状态只由 setup() 改。）

    密钥类字段**永远**剥掉，没有任何开关。

    正文类字段（body 等）默认剥掉，只有 `log_bodies = true` 才放行 ——
    那是一个显式的、需要用户主动去配的逃生开关，存在的唯一理由是
    「正文里明明有 @alice 但正则没抽到」，那种情形消息和日志的
    mentions 会同时为空，光看元数据区分不出来。

    注意放行正文**不等于**放弃防注入：值仍然经过 _quote 的控制字符
    转义，正文里的换行只会变成字面量 \\n，不会把日志劈成两行。
    """
    if allow_bodies is None:
        allow_bodies = _ALLOW_BODIES
    return {k: v for k, v in fields.items() if not _is_forbidden(k, allow_bodies)}


class TextFormatter(logging.Formatter):
    """key=value 文本，给人读、给 grep 用。

    刻意**不做**成 Prometheus 的花括号语法 —— 日志行是人在群晖的
    `docker logs` 里看的。`status=200` 和 `status="200"` 一样好 grep，
    没必要为此牺牲可读性。

    堆栈**必须**在这里输出。text 是默认格式，而这个类之前漏掉了
    exc_info —— 未预期异常只留下一行 `msg="未处理的异常"`，没有任何
    堆栈，恰好在最需要堆栈的时候什么都看不到。堆栈按 C 风格转义成一行
    内的字面量，保证「一条日志一行」不被破坏。
    """

    def format(self, record):
        ts = datetime.fromtimestamp(record.created).astimezone().isoformat(
            timespec="milliseconds")
        parts = [f"ts={ts}", f"lvl={record.levelname}"]
        fields = safe_fields(getattr(record, "relay_fields", None) or {})
        if fields:
            parts.extend(f"{k}={_format_value(v, k)}" for k, v in fields.items())
        if record.getMessage():
            parts.append(f"msg={_quote(record.getMessage())}")
        line = " ".join(parts)
        if record.exc_info:
            trace = self.formatException(record.exc_info).strip()
            # exc_info=True 但当前没有活动异常时，formatException 返回
            # 字面量 "NoneType: None"。把它写进日志只会让人以为真出了
            # 一个 NoneType 的错 —— 噪音比没有更糟。
            if trace and trace != "NoneType: None":
                line += f" exc={_quote(trace)}"
        return line


class JsonFormatter(logging.Formatter):
    """一行一个 JSON，给 log shipper 收。"""

    def format(self, record):
        out = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(
                timespec="milliseconds"),
            "level": record.levelname,
        }
        out.update(safe_fields(getattr(record, "relay_fields", None) or {}))
        if record.getMessage():
            out["msg"] = record.getMessage()
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def get_logger():
    """取 logger。

    setup() 之前也可用 —— 那时只有 stdout handler，配置加载失败这类
    早期错误照样能打出来。
    """
    return logging.getLogger(LOGGER_NAME)


def event(logger, level, message, **fields):
    """记一条结构化日志。

    字段走 `extra` 而不是拼进 message，这样 text 和 json 两种格式
    能共用同一份数据，且字段值不会和 message 里的空格打架。

    `exc_info=True` 会被摘出来交给 logging 自己处理 —— 它是 logging 的
    保留关键字，不能混进 `fields`：直接 `logger.error(msg, layer=...)`
    会让 Logger._log() 收到不认识的 kwarg 并抛 TypeError，而那行代码
    往往就在异常处理器里 —— 处理器自己抛异常，客户端拿到的是连接重置，
    而不是本该给它的 500。
    """
    exc_info = fields.pop("exc_info", False)
    logger.log(level, message, exc_info=exc_info or None,
               extra={"relay_fields": safe_fields(fields)})


def setup(cfg, stream=None):
    """按配置装配 handler。**任何失败都只降级，不抛出。**

    返回一句警告文本（成功则为空串），供调用方决定怎么报。
    """
    global _ALLOW_BODIES
    logger = get_logger()
    level = getattr(logging, str(cfg.level).upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    logger.setLevel(level)
    # 本模块自己的 logger 不往 root 传，否则 stdout 会重复打一遍。
    logger.propagate = False
    _ALLOW_BODIES = bool(cfg.log_bodies)
    # setup 可能被重复调用（测试里尤其），先清掉上一轮留下的 handler。
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa
            pass

    formatter = (JsonFormatter() if cfg.format == "json" else TextFormatter())
    stdout = logging.StreamHandler(stream or sys.stdout)
    stdout.setFormatter(formatter)
    logger.addHandler(stdout)

    if not cfg.file:
        return ""

    try:
        # 与 core.py 的 Seen 同一套路：父目录不存在就建。见本模块顶部的防护 1。
        parent = os.path.dirname(cfg.file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            cfg.file, maxBytes=cfg.max_bytes, backupCount=cfg.backups,
            encoding="utf-8")
    except Exception as e:  # noqa
        return f"日志文件 {cfg.file} 不可用（{type(e).__name__}: {e}），仅输出到 stdout"

    rotating.setFormatter(formatter)
    logger.addHandler(rotating)
    return ""


class RateLimiter:
    """固定窗口限流器。

    存在的理由：401 是**未认证**请求，任何能连到 8090 的人都能零成本刷。
    每一��都记明细等于给了对方一个日志写放大器 —— 填满 5MB 轮转文件，
    把真正的投递记录挤出去，攻击者反而达成目的。

    行为：窗口内前 `limit` 次返回 True（记明细），之后返回 False；
    被抑制的条数攒到一定量时打一行聚合，既保留「有人在试」的检测信号，
    又不让它无上限地占磁盘。
    """

    def __init__(self, limit=5, window=60.0, aggregate_every=25):
        self.limit = limit
        self.window = window
        self.aggregate_every = aggregate_every
        self._lock = threading.Lock()
        self._count = 0
        self._suppressed = 0
        self._window_start = time.monotonic()
        self._since_aggregate = 0

    def allow(self):
        """True = 记明细；False = 已被抑制。线程安全。"""
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= self.window:
                self._window_start = now
                self._count = 0
                self._suppressed = 0
                self._since_aggregate = 0
            if self._count < self.limit:
                self._count += 1
                return True
            self._suppressed += 1
            self._since_aggregate += 1
            return False

    def take_aggregate(self):
        """取走待聚合的条数，没有则返回 0。"""
        with self._lock:
            if self._since_aggregate < self.aggregate_every:
                return 0
            n = self._since_aggregate
            self._since_aggregate = 0
            return n