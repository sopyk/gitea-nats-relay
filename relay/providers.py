"""Provider：把某个平台的原始 webhook 翻译成本服务的统一事件。

一个 provider 声明五件事（头名、验签、路径、事件映射、payload 归一化）。
前三件来自配置；事件映射来自配置；**payload 归一化是代码** —— 各平台 JSON
结构真的不同，这部分没法塞进配置。归一化之后的下游完全平台无关。

注册表按路径索引：HTTP 层拿到请求后按 path 找到 provider，其余逻辑不再关心
这是哪个平台。
"""
import hashlib
import hmac
import re
from datetime import datetime, timezone

# @提及的边界判定。
#
# 这里不能用 \b / \w —— Python 3 的 \w 是 Unicode 感知的，汉字属于 \w，
# 于是「请@bob看」「@bob请看」都会被当成「@ 紧邻单词字符」而丢掉。本项目面向中文团队，
# 那种写法才是常态（旧实现正是栽在这里）。
#
# 规则：@ 前后必须既不是「登录名字符」，也不是「邮箱/路径字符」。
#   - 登录名本体：[A-Za-z0-9_-]（与 Gitea 登录名规则一致）
#   - 邮箱排除：foo@bar.com —— @ 前面是字母
#   - 路径排除：src/@file —— @ 前面是 /
#   - 汉字 / 全角标点 / 空白 → 都是合法边界
#
# 关键：lookaround 里用 [A-Za-z0-9] 而不是 \w —— 前者只认 ASCII，
# 汉字自然放行；同时登录名本来就不含汉字，所以两者边界一致。
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_@.\-/])@([A-Za-z0-9_-]+)(?![A-Za-z0-9_.\-/])")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")

MAX_TITLE_CHARS = 140


def extract_mentions(text):
    """剥离代码块/行内码后抽取 @登录名（代码里的 @ 不触发通知）。"""
    if not text:
        return []
    text = _CODE_FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    return _MENTION_RE.findall(text)


class Provider:
    """配置 + 归一化函数 = 一个平台。"""

    def __init__(self, cfg, normalizer):
        self.cfg = cfg
        self.name = cfg.name
        self.path = cfg.path
        self.normalizer = normalizer

    def verify(self, body, headers):
        secret = self.cfg.signature.secret
        if not secret:
            return False
        provided = headers.get(self.cfg.signature_header, "")
        if not provided:
            return False
        expected = hmac.new(
            secret.encode(), body, getattr(hashlib, self.cfg.signature.hash_name)
        ).hexdigest()
        # 定长比较：非定长的 == 会被时序侧信道逐字节爆破
        return hmac.compare_digest(expected, provided.strip())

    def read_delivery(self, headers):
        return headers.get(self.cfg.delivery_header, "")

    def resolve_kind(self, headers):
        """返回统一事件类型；平台发的事件我们不认则返回 None（→ 200 ignored）。"""
        event = headers.get(self.cfg.event_header, "")
        event_type = (headers.get(self.cfg.event_type_header, "")
                      if self.cfg.event_type_header else "")
        return self.cfg.resolve_kind(event, event_type)

    def normalize(self, payload, kind, action):
        return self.normalizer(payload, kind, action)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _login_list(users):
    return [u.get("login") for u in (users or []) if isinstance(u, dict) and u.get("login")]


def normalize_gitea(payload, kind, action):
    """Gitea / Forgejo payload → 统一事件。

    字段名对照 Gitea `modules/structs/hook.go` 的结构体，踩过三个坑：
      * payload 里**没有** `action_at` 这个字段，时间得自己兜底
      * review 的正文在 `review.content`，**不是** `review.body`
      * `requested_reviewer`（单数）是 review_request 动作专用的，
        和 PR 上的 `requested_reviewers`（复数）不是一回事
    """
    repo = payload.get("repository") or {}
    sender = payload.get("sender") or {}

    # Gitea 的 issue_comment 事件把 PR 放在**顶层** pull_request，
    # 而 issue 对象上那个 pull_request 只是个 URL 摘要。两者都看一眼。
    issue_obj = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    is_pr = bool(payload.get("pull_request")) or bool(issue_obj.get("pull_request"))
    obj = issue_obj or payload.get("pull_request") or {}

    # review_request 动作时，被请求的那个人在顶层单数字段里。
    # 不把它算进收件人，「请求评审」就通知不到人。
    reviewers = list(_login_list(obj.get("requested_reviewers")))
    reviewer = (payload.get("requested_reviewer") or {}).get("login")
    if reviewer and reviewer not in reviewers:
        reviewers.append(reviewer)

    ev = {
        "kind": kind,
        "action": action,
        "repo": repo.get("full_name", ""),
        "actor": sender.get("login", ""),
        # Gitea 的 webhook payload 里根本没有时间字段（旧代码读一个不存在的
        # action_at，永远拿到空串）。这里退回到本地时钟 —— 至少让下游能排序。
        "ts": payload.get("action_at") or _now_iso(),
    }

    if obj:
        ev["number"] = obj.get("number")
        ev["title"] = (obj.get("title") or "")[:MAX_TITLE_CHARS]
        ev["url"] = obj.get("html_url") or ""
        ev["state"] = obj.get("state")
        ev["assignees"] = _login_list(obj.get("assignees"))
        if is_pr or kind in ("pull_request", "review"):
            ev["is_pr"] = True
        ev["labels"] = [l.get("name") for l in (obj.get("labels") or [])
                        if isinstance(l, dict) and l.get("name")]

    c = payload.get("comment") or {}
    if c:
        ev["comment_url"] = c.get("html_url", "")
        # Gitea 的 Comment 结构体**没有** mentions 字段（API 里有，webhook 里没有），
        # 所以提及只能靠正文正则抽。留着一行兼容将来可能的字段。
        api = _login_list(c.get("mentions"))
        ev["mentions"] = sorted(set(api) | set(extract_mentions(c.get("body") or "")))

    rev = payload.get("review") or {}
    if rev:
        # Gitea 的 ReviewPayload 是 {"type": ..., "content": ...}，
        # 读 body 会永远拿到 None，评审正文里的 @提及 100% 丢失。
        ev["review_state"] = rev.get("type") or rev.get("state")
        api = _login_list(rev.get("mentions"))
        ev["mentions"] = sorted(set(api) | set(extract_mentions(rev.get("content") or "")))

    if reviewers:
        ev["reviewers"] = sorted(set(reviewers))

    return ev


NORMALIZERS = {"gitea": normalize_gitea}


def build_registry(cfg):
    """按路径索引 provider。路径重复是配置错误 —— 一个路径只能有一个归属。"""
    registry = {}
    for p in cfg.enabled_providers():
        norm = NORMALIZERS.get(p.name)
        if norm is None:
            raise ValueError(
                f"provider {p.name!r} 没有对应的 payload 归一化实现；"
                f"已内置: {', '.join(sorted(NORMALIZERS))}")
        if p.path in registry:
            raise ValueError(
                f"路径 {p.path!r} 被 {registry[p.path].name!r} 和 {p.name!r} 同时占用")
        registry[p.path] = Provider(p, norm)
    return registry