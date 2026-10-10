"""配置加载与校验。

配置来源优先级：**环境变量 > 配置文件 > 代码默认值**。

保留环境变量不是 12-factor 的执念，而是过渡期的必要：TOML 落地前 NAS 上跑的是
env 注入的部署；TOML 落地后两边同时可用，env 继续承担容器 secret 的角色。

校验在启动时一次做完，**宁可起不来也不要带着错配置跑**——这个服务最贵的失败模式
不是挂掉，而是静默丢事件、通知错人。
"""
import json
import os
import tomllib
from dataclasses import dataclass, field

# 出站统一事件的 kind 白名单。改动这里等于改出站契约，所有已订阅的 Agent 都要
# 跟着改 —— 必须是有意识的动作，且应该先改这里再改文档。
VALID_KINDS = frozenset({"issue", "issue_comment", "pull_request", "review"})

VALID_SIG_ALGOS = {"hmac-sha256": "sha256", "hmac-sha1": "sha1", "hmac-sha512": "sha512"}

DEFAULT_CONFIG_FILE = "/config/webhook2nats.toml"

VALID_TOP_LEVEL = frozenset({"service", "nats", "routing", "dedup", "providers"})
VALID_SERVICE = frozenset(
    {"listen", "client_name", "relay_id", "heartbeat_seconds", "publish_timeout_seconds"})
VALID_NATS = frozenset({"servers", "user", "password"})
VALID_ROUTING = frozenset(
    {"members_file", "notify_subject_prefix", "broadcast_subject", "broadcast_authors"})
VALID_DEDUP = frozenset({"file", "capacity"})
VALID_PROVIDER = frozenset({"enabled", "path", "headers", "signature", "events", "event_types"})
VALID_HEADERS = frozenset({"event", "event_type", "delivery", "signature"})
VALID_SIGNATURE = frozenset({"algorithm", "secret"})


class ConfigError(Exception):
    """配置不合法。启动时抛出，直接终止 —— 绝不带着错配置跑。"""


@dataclass
class Signature:
    algorithm: str = "hmac-sha256"
    secret: str = ""

    @property
    def hash_name(self):
        return VALID_SIG_ALGOS.get(self.algorithm)


@dataclass
class ProviderConfig:
    """一个平台的接入声明。头名、验签、事件映射全部来自配置，不硬编码。"""
    name: str
    enabled: bool = True
    path: str = "/"
    event_header: str = "X-Event"
    event_type_header: str | None = None
    delivery_header: str = "X-Delivery"
    signature_header: str = "X-Signature"
    signature: Signature = field(default_factory=Signature)
    events: dict = field(default_factory=dict)
    event_types: dict = field(default_factory=dict)

    def resolve_kind(self, event, event_type):
        """细头表优先，粗头表兜底，都没命中返回 None。

        先查细头是因为它信息量更大：Gitea 的 pull_request_review_request 在粗头里
        和「新开 PR」「推 commit」一样都是 pull_request，只有细头能区分。
        """
        if event_type and self.event_type_header:
            kind = self.event_types.get(event_type)
            if kind:
                return kind
        if event:
            return self.events.get(event)
        return None


@dataclass
class Config:
    listen: str = "0.0.0.0:8090"
    client_name: str = "webhook2nats"
    relay_id: str = ""
    heartbeat_seconds: int = 60
    publish_timeout_seconds: float = 3.0

    nats_servers: list = field(default_factory=list)
    nats_user: str = ""
    nats_password: str = ""

    members_file: str = "/config/members.json"
    notify_subject_prefix: str = "evt.gitea.notify"
    broadcast_subject: str = "evt.gitea.broadcast"
    broadcast_authors: set = field(default_factory=set)

    dedup_file: str = "/data/seen.json"
    dedup_capacity: int = 2000

    providers: dict = field(default_factory=dict)

    def enabled_providers(self):
        return [p for p in self.providers.values() if p.enabled]


def _reject_unknown(section_name, data, allowed, where):
    """TOML 只校验语法不校验 schema —— 表名拼错会被当成一张无关的新表，
    配置静默失效。所以这里必须显式拦住未知键。"""
    unknown = set(data) - set(allowed)
    if unknown:
        raise ConfigError(
            f"{where} 出现未知配置项: {', '.join(sorted(unknown))}\n"
            f"合法项: {', '.join(sorted(allowed))}\n"
            f"（表名/键名拼错不会报错，只会静默失效 —— 这里替你拦住）")


def _parse_file(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置文件解析失败 {path}: {e}") from None


def _apply_env(cfg, env):
    """环境变量覆盖。变量名保持与旧版本完全一致，避免 NAS 上已有部署被打断。"""
    def s(k):
        return env.get(k, "").strip()

    if s("NATS_SERVERS"):
        cfg.nats_servers = [x.strip() for x in s("NATS_SERVERS").split(",") if x.strip()]
    if s("NATS_USER"):
        cfg.nats_user = s("NATS_USER")
    if s("NATS_PASSWORD"):
        cfg.nats_password = s("NATS_PASSWORD")
    if s("WEBHOOK_SECRET"):
        for p in cfg.providers.values():
            p.signature.secret = s("WEBHOOK_SECRET")
    if s("MEMBERS_FILE"):
        cfg.members_file = s("MEMBERS_FILE")
    if s("DEDUP_FILE"):
        cfg.dedup_file = s("DEDUP_FILE")
    if s("RELAY_ID"):
        cfg.relay_id = s("RELAY_ID")
    if s("RELAY_CLIENT_NAME"):
        cfg.client_name = s("RELAY_CLIENT_NAME")
    if s("NATS_HEARTBEAT_SECONDS"):
        cfg.heartbeat_seconds = int(s("NATS_HEARTBEAT_SECONDS"))
    if s("LISTEN"):
        cfg.listen = s("LISTEN")
    if s("PUBLISH_TIMEOUT_SECONDS"):
        cfg.publish_timeout_seconds = float(s("PUBLISH_TIMEOUT_SECONDS"))
    if s("BROADCAST_AUTHORS"):
        cfg.broadcast_authors = {x.strip() for x in s("BROADCAST_AUTHORS").split(",") if x.strip()}
    if s("NOTIFY_SUBJECT_PREFIX"):
        cfg.notify_subject_prefix = s("NOTIFY_SUBJECT_PREFIX")
    if s("BROADCAST_SUBJECT"):
        cfg.broadcast_subject = s("BROADCAST_SUBJECT")
    return cfg


def _build_provider(name, raw, warn):
    _reject_unknown(f"[providers.{name}]", raw, VALID_PROVIDER, f"[providers.{name}]")

    headers = raw.get("headers", {})
    _reject_unknown(f"[providers.{name}.headers]", headers, VALID_HEADERS,
                    f"[providers.{name}.headers]")
    sig_raw = raw.get("signature", {})
    _reject_unknown(f"[providers.{name}.signature]", sig_raw, VALID_SIGNATURE,
                    f"[providers.{name}.signature]")

    p = ProviderConfig(
        name=name,
        enabled=raw.get("enabled", True),
        path=raw.get("path", "/"),
        event_header=headers.get("event", "X-Event"),
        # 细头是可选的：GitHub 这类只有一个事件头的平台不声明即可，
        # 整张 event_types 表随之不生效，退化成单表匹配。
        event_type_header=headers.get("event_type"),
        delivery_header=headers.get("delivery", "X-Delivery"),
        signature_header=headers.get("signature", "X-Signature"),
        signature=Signature(
            algorithm=sig_raw.get("algorithm", "hmac-sha256"),
            secret=sig_raw.get("secret", ""),
        ),
        events=dict(raw.get("events", {})),
        event_types=dict(raw.get("event_types", {})),
    )

    for table in ("events", "event_types"):
        for key, kind in p.events.items() if table == "events" else p.event_types.items():
            if kind not in VALID_KINDS:
                raise ConfigError(
                    f"[providers.{name}.{table}] {key} 映射到未文档化的 kind {kind!r}；"
                    f"合法值: {', '.join(sorted(VALID_KINDS))}")
    # 只写「与粗头表结果不同」的条目是正常用法，不告警。
    # 真正的坑是同一个键在两张表里值不一样 —— 改 [events] 忘了改 [event_types]，
    # 细头表会静默压掉粗头表的修改，让人以为改了却没生效。
    for key, kind in p.event_types.items():
        if key in p.events and p.events[key] != kind:
            warn(f"[{name}] events.{key}={p.events[key]!r} 与 "
                 f"event_types.{key}={kind!r} 不一致 —— event_types 优先，"
                 f"实际生效的是 {kind!r}")
    if p.event_types and not p.event_type_header:
        raise ConfigError(
            f"[providers.{name}] 配了 event_types 表却没有声明 headers.event_type —— "
            f"整张细头表将永不生效")
    return p


def _defaults():
    """没有任何配置文件时的兜底：与旧版本硬编码完全一致。"""
    cfg = Config()
    cfg.nats_servers = []
    cfg.providers["gitea"] = ProviderConfig(
        name="gitea",
        path="/gitea-hook",
        event_header="X-Gitea-Event",
        event_type_header=None,  # 旧代码只读一个头，保持行为不变
        delivery_header="X-Gitea-Delivery",
        signature_header="X-Gitea-Signature",
        events={"issues": "issue", "issue_comment": "issue_comment",
                "pull_request": "pull_request"},
    )
    return cfg


def load(path=None, env=None, warn=None):
    """加载配置：文件 → 环境变量覆盖 → 校验。

    warn 是一个收集告警的回调（默认打印），用来报告那些不该致命、
    但很可能意味着配置写错了的问题。
    """
    env = os.environ if env is None else env
    path = path or env.get("CONFIG_FILE", "") or DEFAULT_CONFIG_FILE
    warnings = []
    warn = warn or (lambda m: warnings.append(m))

    raw = _parse_file(path)
    cfg = _defaults() if not raw else Config()

    if raw:
        _reject_unknown("配置文件根", raw, VALID_TOP_LEVEL, "配置文件根")

        svc = raw.get("service", {})
        _reject_unknown("[service]", svc, VALID_SERVICE, "[service]")
        for k in ("listen", "client_name", "relay_id"):
            if k in svc:
                setattr(cfg, k, svc[k])
        if "heartbeat_seconds" in svc:
            cfg.heartbeat_seconds = int(svc["heartbeat_seconds"])
        if "publish_timeout_seconds" in svc:
            cfg.publish_timeout_seconds = float(svc["publish_timeout_seconds"])

        nat = raw.get("nats", {})
        _reject_unknown("[nats]", nat, VALID_NATS, "[nats]")
        cfg.nats_servers = nat.get("servers", cfg.nats_servers)
        cfg.nats_user = nat.get("user", cfg.nats_user)
        cfg.nats_password = nat.get("password", cfg.nats_password)

        rt = raw.get("routing", {})
        _reject_unknown("[routing]", rt, VALID_ROUTING, "[routing]")
        if "members_file" in rt:
            cfg.members_file = rt["members_file"]
        cfg.notify_subject_prefix = rt.get("notify_subject_prefix", cfg.notify_subject_prefix)
        cfg.broadcast_subject = rt.get("broadcast_subject", cfg.broadcast_subject)
        if "broadcast_authors" in rt:
            cfg.broadcast_authors = set(rt["broadcast_authors"])

        dd = raw.get("dedup", {})
        _reject_unknown("[dedup]", dd, VALID_DEDUP, "[dedup]")
        cfg.dedup_file = dd.get("file", cfg.dedup_file)
        cfg.dedup_capacity = int(dd.get("capacity", cfg.dedup_capacity))

        for name, praw in raw.get("providers", {}).items():
            cfg.providers[name] = _build_provider(name, praw, warn)

    _apply_env(cfg, env)
    cfg.warnings = warnings
    validate(cfg)
    return cfg


def validate(cfg):
    missing = [n for n, v in (("NATS_SERVERS", cfg.nats_servers),
                              ("NATS_USER", cfg.nats_user),
                              ("NATS_PASSWORD", cfg.nats_password)) if not v]
    for p in cfg.enabled_providers():
        if not p.signature.secret:
            missing.append(f"WEBHOOK_SECRET（provider {p.name} 的 signature.secret）")
        if p.signature.algorithm not in VALID_SIG_ALGOS:
            raise ConfigError(
                f"provider {p.name}: 不支持的验签算法 {p.signature.algorithm!r}；"
                f"合法值: {', '.join(sorted(VALID_SIG_ALGOS))}")
        if not p.events and not p.event_types:
            raise ConfigError(f"provider {p.name}: events 和 event_types 都是空的，收不到任何事件")
    if not cfg.enabled_providers():
        raise ConfigError("没有启用任何 provider")
    if missing:
        raise ConfigError("缺少必填配置: " + ", ".join(missing))


def load_members(path):
    """读成员映射。**逐条校验**：历史上这里是 `m.get("host")` 直接调用，
    映射里混进一个非 dict 值就会让每一次 webhook 都抛 AttributeError，
    而这个文件是只读挂载的，运维改不了只能干瞪眼。"""
    members = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return members
    except ValueError as e:
        raise ConfigError(f"成员映射 {path} 不是合法 JSON: {e}") from None

    raw = data.get("members", data) if isinstance(data, dict) else {}
    if not isinstance(raw, dict):
        raise ConfigError(f"成员映射 {path}: members 段必须是对象")

    bad = []
    for login, v in raw.items():
        if not isinstance(v, dict):
            bad.append(f"{login}（应为 {{host, id}}，实际是 {type(v).__name__}）")
            continue
        host, ident = v.get("host"), v.get("id")
        if not (isinstance(host, str) and host.strip()
                and isinstance(ident, str) and ident.strip()):
            bad.append(f"{login}（host/id 缺失或非字符串）")
            continue
        members[login] = {"host": host.strip(), "id": ident.strip()}
    if bad:
        raise ConfigError(
            f"成员映射 {path} 有 {len(bad)} 条不合法，已全部拒绝：\n  "
            + "\n  ".join(bad))
    return members