# gitea-nats-relay

A small, dependency-light webhook relay that turns **Gitea** events into **Core NATS** "doorbell" messages — so subscribed autonomous agents are woken in real time when they are `@mentioned`, assigned, or requested for review, instead of polling Gitea on a timer.

- **Runtime:** Python 3.12 + [`nats-py`](https://github.com/nats-io/nats.py), shipped as a tiny Alpine image.
- **Transport:** plain **Core NATS** (instant, at-most-once, in-memory). No JetStream, no message history, no database.
- **Scope:** notification only. It does **not** persist bodies, mark notifications read, or act as any agent. Gitea remains the single source of truth; after being nudged, each agent fetches the full content from Gitea itself.

## Why

A fleet of agents was polling a self-hosted Gitea every few minutes — many empty requests, and latency up to the poll interval. The relay sits between Gitea and NATS: Gitea pushes one webhook per event; the relay validates, shrinks, and resolves the recipients; then publishes a compact "doorbell" on NATS. Online agents receive it within milliseconds. Offline agents simply miss that nudge (by design) and are covered by a separate, infrequent Gitea sweep that reconciles against Gitea unread state.

## Architecture

```
┌──────────────┐  webhook (HTTP POST, HMAC-SHA256)   ┌────────────────────┐  Core NATS   ┌──────────────────┐
│    Gitea     │ ───────────────────────────────────▶│  gitea-nats-relay  │ ───────────▶ │ subscribed       │
│ (source of   │  issues / issue_comment /           │  (this service)    │  doorbell    │ agents (online)  │
│  truth)      │  pull_request / pull_request_review │                    │  subjects    └──────────────────┘
└──────────────┘                                      └────────────────────┘
        ▲                                                      │
        │  agents fetch full issue/PR content themselves       │ dedup state (local file)
        └──────────────────────────────────────────────────────┘
```

Per event:

1. **Gitea** emits a webhook.
2. **relay** verifies the HMAC signature, de-duplicates by delivery UUID, and resolves recipients (mentions / assignees / requested reviewers) against an **external** member map, then publishes a compact JSON event (kept well under 1 KB).
3. **NATS** fans it out to the matching online subscriber.

If no subscriber is online the Core NATS message is dropped by design — the infrequent Gitea sweep is the guaranteed catch-up. The relay deliberately is not durable storage.

## Subject contract

The relay's NATS account is server-side ACL-scoped so it can **only** publish these:

| Subject | Meaning |
|---|---|
| `<NOTIFY_SUBJECT_PREFIX>.<host>.<id>` | Targeted nudge to one agent (default prefix `evt.gitea.notify`). |
| `<BROADCAST_SUBJECT>` | Council-wide announcement (default `evt.gitea.broadcast`), only for configured authors opening an issue tagged/titled as an announcement. |
| `hb.<RELAY_ID>` | Service heartbeat (sent only when `RELAY_ID` is set). |

Identity (`<host>.<id>`) is encoded in the subject, and each agent's server-side permission template locks it to its own inbox. Spoofing another identity therefore fails at NATS authorization rather than relying on application-level honesty.

## Message shape

Metadata only — the agent fetches full content from `url`:

```json
{
  "kind": "issue_comment",
  "action": "created",
  "repo": "org/repo",
  "actor": "sender-login",
  "ts": "2026-10-10T09:30:00Z",
  "number": 134,
  "title": "…",
  "url": "https://<gitea>/<org>/<repo>/issues/134",
  "state": "open",
  "assignees": ["login"],
  "labels": ["…"],
  "comment_url": "https://<gitea>/…/issues/134#issuecomment-42",
  "mentions": ["login"],
  "delivery": "gitea-delivery-uuid"
}
```

## Configuration — environment-specific values are externalized

The image contains **only code and dependencies**. No IP addresses, hosts, domains, accounts, secrets, or member identities are baked in. The member map is a read-only mounted file; secrets come from an env file.

| Variable | Required | Description |
|---|---|---|
| `NATS_SERVERS` | yes | Comma-separated NATS URLs, e.g. `nats://<nats-host>:4222`. |
| `NATS_USER` / `NATS_PASSWORD` | yes | Relay-specific NATS account, ACL-limited to doorbell subjects. |
| `WEBHOOK_SECRET` | yes | HMAC-SHA256 secret, identical to the Gitea webhook config. |
| `MEMBERS_FILE` | no | Path to login→`{host,id}` JSON, default `/config/members.json` (mounted read-only). |
| `DEDUP_FILE` | no | Delivery de-dup state, default `/data/seen.json` (writable volume). |
| `RELAY_ID` | no | Heartbeat suffix (`hb.<RELAY_ID>`); unset disables heartbeat. |
| `RELAY_CLIENT_NAME` | no | NATS connection display name. |
| `NATS_HEARTBEAT_SECONDS` | no | Heartbeat interval, default `60`. |
| `LISTEN` | no | HTTP listen address, default `0.0.0.0:8090`. |
| `PUBLISH_TIMEOUT_SECONDS` | no | Wait for NATS publish+flush, default `3`. |
| `BROADCAST_AUTHORS` | yes* | Comma-separated logins whose announcement-tagged/titled new issues fan out widely. |
| `NOTIFY_SUBJECT_PREFIX` | no | Default `evt.gitea.notify`. |
| `BROADCAST_SUBJECT` | no | Default `evt.gitea.broadcast`. |

\* There is no built-in default author list; unset means no broadcasts.

Example `members.json` (mounted at `/config/members.json`, **not** in the image):

```json
{
  "members": {
    "alice": { "host": "h1", "id": "alice" },
    "bob":   { "host": "h2", "id": "bob" }
  }
}
```

## HTTP contract

- `GET /healthz` → `200 ok` when connected to NATS, otherwise `503 nats-down`.
- `POST /gitea-hook` (the target URL configured in Gitea):
  - `401 bad-signature` — HMAC mismatch or missing `WEBHOOK_SECRET`.
  - `200 duplicate` — delivery UUID already **successfully** processed.
  - `409 inflight` — the same delivery is being handled by an overlapping retry.
  - `400 bad-json` — malformed body.
  - `502 publish-failed` / `504 publish-timeout` / `500 publish-error` — NATS did not confirm; the delivery is **rolled back** so Gitea's retry reuses the same UUID instead of being dropped as a duplicate.
  - `200 accepted` — every relevant subject was published and flushed; only then is the delivery marked seen.

### De-duplication & retry semantics

A delivery UUID enters an `inflight` state and is persisted as seen **only after** every NATS publish+flush succeeds. On failure/timeout it is rolled back, letting Gitea safely retry the same `X-Gitea-Delivery`. Overlapping retries of the same UUID receive `409`. State is an LRU (default 2000) persisted atomically to disk, so a restart does not re-notify.

### Mention parsing

`@login` tokens are extracted **after** fenced and inline code blocks are stripped, so an `@bob` inside backticks or a code fence does not trigger a notification. Only logins present in the member map produce a targeted subject; unknown names are ignored for routing (but kept in the event's `mentions` for visibility).

## Security notes

- The relay holds **no Gitea token** and no NATS account that can read messages or post as an agent — the relay account is publish-only on three subjects. A compromise is limited to forging doorbells, which carry no content beyond public metadata and URLs.
- Webhooks are authenticated by HMAC-SHA256; the listener is intended to live on a private LAN / tailnet and is not exposed publicly.
- Secrets live in an operator-managed env file (`0600`); the member map is a read-only mount. Neither enters the image or this repository.

## Testing

No NATS or network required — the suite fakes the NATS client and drives the HTTP handler in-process:

```bash
cd relay
python3 test_relay.py
```

Covers HMAC rejection, code-span-safe mention extraction, member-map targeting, broadcast detection, delivery dedup round-trip, failed-delivery retry, inflight rejection, and end-to-end webhook handling.

## Project layout

```
relay/
  server.py          # the service
  test_relay.py      # behavior tests (stdlib + fake NATS)
  Dockerfile         # code-only image; config mounted at runtime
.github/workflows/   # builds linux/amd64 and pushes to GHCR on tag relay-v*
```

Deployment artifacts (compose, env templates, NATS ACL snippets, the real member map) carry environment-specific values and are deliberately kept in the operator's internal repo, not in this public image.

## Design decisions

- **No JetStream.** Agents need an instant nudge; Gitea unread state is the authoritative reconciliation source for anything missed offline. A durable work queue would duplicate what Gitea already stores.
- **Notification, not action.** No Gitea write token; cannot mark read or speak for an agent.
- **Config over image.** All environment-specific values are mounted/injected, so the same immutable image runs anywhere and is safe to publish publicly.
