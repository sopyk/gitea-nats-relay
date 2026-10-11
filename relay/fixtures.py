#!/usr/bin/env python3
"""真实 Gitea payload fixture。

字段严格对照 Gitea `modules/structs/hook.go` 的结构体，**不是凭印象编的**。
这几个结构体里藏着本项目踩过的坑，所以在测试里原样复现：

* `IssueCommentPayload` / `IssuePayload` / `PullRequestPayload` **都没有 `action_at`**
  字段 —— 旧代码读它，永远拿到空串
* `ReviewPayload` 是 `{"type", "content"}`，**没有 `body` 也没有 `state`**
* `Comment` **没有 `mentions` 字段**（webhook 里没有，REST API 里才有）
* `PullRequestPayload` 的 `RequestedReviewer` 是**单数**，且在**顶层**，
  和 PR 对象上的 `RequestedReviewers`（复数）不是一回事
"""
import copy


def _user(login):
    return {"id": 1, "login": login, "username": login}


def _repo(full_name="org/repo"):
    org, _, name = full_name.partition("/")
    return {
        "id": 1,
        "name": name,
        "full_name": full_name,
        "html_url": f"https://gitea.example/{org}/{name}",
        "owner": _user(org),
    }


def _issue(number=134, title="评审流水线的重试策略", pull=False):
    obj = {
        "id": 900,
        "number": number,
        "title": title,
        "html_url": f"https://gitea.example/org/repo/issues/{number}",
        "state": "open",
        "body": "讨论一下",
        "assignees": [_user("carol")],
        "labels": [{"id": 1, "name": "bug"}],
        "user": _user("you"),
        "created_at": "2026-10-10T09:00:00Z",
        "updated_at": "2026-10-10T09:30:00Z",
    }
    if pull:
        obj["pull_request"] = {
            "merged": False,
            "html_url": f"https://gitea.example/org/repo/pulls/{number}",
            "base": {"ref": "main"},
            "head": {"ref": "feature"},
        }
    return obj


def _pull_request(number=7, reviewers=("dave",)):
    return {
        "id": 700,
        "number": number,
        "title": "把重试上限改成 3",
        "html_url": f"https://gitea.example/org/repo/pulls/{number}",
        "state": "open",
        "draft": False,
        "body": "描述",
        "labels": [{"id": 1, "name": "bug"}],
        "assignees": [_user("carol")],
        "requested_reviewers": [_user(r) for r in reviewers],
        "user": _user("you"),
        "created_at": "2026-10-10T09:00:00Z",
        "updated_at": "2026-10-10T09:30:00Z",
    }


# ── IssueCommentPayload ────────────────────────────────────────
def issue_comment(body="请 @carol 看一下，@dave 帮忙确认",
                  issue=None, is_pull=False):
    p = {
        "action": "created",
        "issue": issue if issue is not None else _issue(),
        "comment": {
            "id": 42,
            "html_url": "https://gitea.example/org/repo/issues/134#issuecomment-42",
            "pull_request_url": "",
            "issue_url": "https://gitea.example/api/v1/issues/134",
            "user": _user("you"),
            "body": body,
            "assets": [],
            "created_at": "2026-10-10T09:30:00Z",
            "updated_at": "2026-10-10T09:30:00Z",
        },
        "repository": _repo(),
        "sender": _user("you"),
        "is_pull": is_pull,
    }
    if is_pull:
        p["pull_request"] = _pull_request()
    return copy.deepcopy(p)


# ── IssuePayload（注意：没有 action_at / 没有时间戳顶层字段）──────
def issue_event(action="opened", issue=None, actor="carol"):
    return copy.deepcopy({
        "action": action,
        "number": (issue or _issue()).get("number", 134),
        "issue": issue if issue is not None else _issue(),
        "repository": _repo(),
        "sender": _user(actor),
        "commit_id": "",
    })


# ── PullRequestPayload ─────────────────────────────────────────
def pull_request_event(action="opened", pr=None, requested_reviewer=None,
                       review=None, actor="carol"):
    p = {
        "action": action,
        "number": (pr or _pull_request()).get("number", 7),
        "pull_request": pr if pr is not None else _pull_request(),
        "repository": _repo(),
        "sender": _user(actor),
        "commit_id": "",
    }
    if requested_reviewer is not None:
        p["requested_reviewer"] = _user(requested_reviewer)
    if review is not None:
        p["review"] = review
    return copy.deepcopy(p)


# ── 评审：ReviewPayload 是 {"type","content"}，没有 body / state ──
def review_approved(content="整体没问题。@carol 可以合了"):
    return pull_request_event(
        action="reviewed",
        review={"type": "pull_request_review_approved", "content": content},
    )


def review_rejected(content="这个重试逻辑有问题，@dave 再看看"):
    return pull_request_event(
        action="reviewed",
        review={"type": "pull_request_review_rejected", "content": content},
    )


def review_comment(content="这里建议改成 `@carol` 那样写"):
    return pull_request_event(
        action="reviewed",
        review={"type": "pull_request_review_comment", "content": content},
    )


# ── 请求评审：requested_reviewer 在顶层且是单数 ─────────────────
def review_requested(reviewer="dave"):
    return pull_request_event(
        action="review_requested",
        requested_reviewer=reviewer,
        # 关键：PR 上的 requested_reviewers 此时是空的。
        # 真正被请求的人只在顶层那个单数字段里。
        pr=_pull_request(reviewers=()),
    )