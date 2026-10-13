# Webhook2NATS

**English README · [中文版](README.md)**

A lightweight **Webhook → Core NATS** realtime "doorbell" relay.

A webhook platform (Gitea / Forgejo / GitHub / GitLab / anything that emits
webhooks) fires events at this channel, which **verifies the signature,
deduplicates, and resolves who should be notified**, then publishes a small
metadata-only event to NATS so a subscribed agent is woken up in
**milliseconds** — instead of polling on a timer.

- **Runtime**: Python 3.12 + `nats-py`, packed into a tiny Alpine image.
- **Transport**: pure **Core NATS** (immediate, at-most-once, in-memory).
  **No JetStream, no message history, no database.**
- **Scope**: notification only. **It does not store bodies, mark things read,
  or act on an agent's behalf.** The source platform remains the source of truth.

> 📖 **Full documentation is in Chinese.** The links below point to the Chinese
> docs; this file is an English summary covering what you need to run it.
> Details on architecture, integration, and operations live in `docs/`.

---

## Image

```bash
docker pull ghcr.io/sopyk/webhook2nats:latest
```

| | |
|---|---|
| Registry | **`ghcr.io/sopyk/webhook2nats`** (GitHub Container Registry, public, anonymous pulls) |
| Architectures | `linux/amd64` + `linux/arm64` |
| Stable | `:latest`, `:1.0`, `:1.0.0`, `v1.0.0` |
| Pre-release | e.g. `:X.Y.Z-rcN` — **does not move** `:latest` |

`:latest` follows stable releases only. Pre-release tags never move it, so a
`docker compose pull` always gets a published stable image. CI enforces this:
if a pre-release build produced `latest`, the build fails.

To find out which version you are running:

```bash
curl -s localhost:8090/version
# {"status":"ok","version":"1.0.0","commit":"22c6f52…","started_at":…,"uptime_seconds":…}
```

---

## What problem it solves

A team of agents polling a self-hosted code platform every few minutes produces
mostly empty requests, with latency bounded by the polling interval.

```
   Before                                  After
┌────────────┐  poll     ┌────────────┐    ┌────────────┐ webhook   ┌──────────────┐ bell      ┌────────────┐ publish   ┌────────────┐
│   Agent    │ ------>   │   Gitea    │    │  platform  │ ------>   │ Webhook2NATS │ ----->    │    NATS    │ ----->    │   Agent    │
└────────────┘ minutes   └────────────┘    └────────────┘ ms        └──────────────┘ instant   └────────────┘ instant   └────────────┘
  latency <= one poll cycle                              latency <= one round trip
  most requests are empty                                                    zero empty requests
```

**A bell, not the content.** Only `repo / number / actor / mentions / url /
delivery` ride on NATS; the agent fetches the full content itself. So even a
forged bell carries nothing but public metadata and URLs.

**Dedup guarantees "not processed twice", not "delivered exactly once"** — see
[failure and retry semantics](docs/architecture.md#失败了会怎样).

---

## Run it in three minutes

```bash
# 1) two files that .gitignore keeps out of the repo
cp relay.env.example relay.env      # NATS account + NATS_PASSWORD + WEBHOOK_SECRET
mkdir -p config
#    config/members.json holds  login → {host, id}  (decides who gets bells)
#    optional: cp config/webhook2nats.example.toml config/webhook2nats.toml

# 2) start
docker compose up -d

# 3) verify
curl -s localhost:8090/healthz        # → {"status": "ok", ...}
```

Build from source instead: `docker compose -f docker-compose.build.yml up -d --build`.

For mount layout, image tag trade-offs, and the **single-instance constraint**
(dedup state is local; one webhook callback must point at one relay — multiple
instances would duplicate deliveries with no error anywhere), see the
[operations manual](docs/operations.md).

---

## Basic usage

**Subscriber side** (your agent) — subscribe first, confirm you are online,
then act on the platform:

```bash
nats sub 'evt.>'
```

> ⚠️ Two things to know upfront, both of which have made verification look like
> a broken service:
> 1. **Subscribe before triggering.** Core NATS keeps no history — if nobody is
>    listening at the moment of publish, the message is gone forever, while the
>    relay still answers `accepted` (it really did publish it).
> 2. **Subscribe to `evt.>`, not `evt.gitea.notify.>`.** Directed bells
>    (`evt.gitea.notify.<host>.<id>`) and broadcasts (`evt.gitea.broadcast`) do
>    not share a namespace — subscribe too narrowly and you silently miss
>    broadcasts, with no error.

**Producer side** (Gitea): point the webhook callback at
`http://<relay>:8090/gitea-hook` and use the same `WEBHOOK_SECRET` as in
`relay.env`.

**You can verify without Gitea** — send a correctly signed request by hand:

```bash
nats sub 'evt.gitea.notify.h1.alice' &

cat > /tmp/hook.json <<'JSON'
{"action":"created",
 "repository":{"full_name":"org/repo"},
 "sender":{"login":"you"},
 "issue":{"number":134,"title":"t",
          "html_url":"https://g.example/org/repo/issues/134","state":"open","labels":[]},
 "comment":{"html_url":"https://g.example/org/repo/issues/134#issuecomment-42",
            "body":"@alice take a look"}}
JSON

SIG=$(openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" /tmp/hook.json | sed 's/^.* //')

curl -i -X POST http://127.0.0.1:8090/gitea-hook \
  -H "X-Gitea-Event: issue_comment" \
  -H "X-Gitea-Delivery: 11111111-2222-3333-4444-555555555555" \
  -H "X-Gitea-Signature: $SIG" \
  --data-binary @/tmp/hook.json
```

You get `{"status": "accepted", ...}` and the bell JSON on the subscriber.
**Send the same `X-Gitea-Delivery` again** → `{"status": "duplicate"}` with
no new message — the quickest local proof that dedup works.

> Use `--data-binary`, not `-d`: the latter strips newlines from the file, so
> the bytes you sign no longer match the bytes you send — a permanent 401.

---

## Core capabilities

| | |
|---|---|
| **Any platform** | header names, signature algorithm, event mapping, and route path all come from config — adding a platform needs no code change |
| **Idempotent** | delivery-UUID dedup: inflight reservation → publish confirmed → persisted; rollback on failure lets the platform retry safely |
| **Chinese `@mentions`** | matches adjacent CJK characters; `@` inside code blocks, emails, and paths is not a mention |
| **No bodies stored** | messages carry metadata + URL only |
| **Config externalized** | zero environment values in the image; one immutable image runs anywhere |
| **Observable** | one structured line per request, keyed by `delivery`, joinable with the NATS message |
| **Logging degrades** | an unwritable log dir falls back to stdout with a warning — the service still starts |

Full per-item status (including known limits):
[`docs/reference.md`](docs/reference.md#实现状态).

---

## Troubleshooting

**Start with the logs** — one line per request, and `delivery` is the same UUID
the agent sees in its message:

```bash
docker logs webhook2nats 2>&1 | grep 'delivery=<uuid>'
```

`status=200 decision=accepted` means the relay side is healthy and the problem
is on the subscriber side (usually subscribe timing). Field reference, what the
logs deliberately omit, and what it means when someone appears in `mentions`
but not in `subjects`: see the
[operations manual](docs/operations.md#日志).

---

## Documentation

| Doc | Covers | For |
|---|---|---|
| [Architecture & design](docs/architecture.md) | why it is built this way, one event end to end, trade-offs | everyone |
| [Adding a platform](docs/integration.md) | the six things a new platform declares, Gitea's event headers, config pitfalls | extenders |
| [Reference](docs/reference.md) | all protocol contracts: subjects, message format, HTTP, config keys, status table | agent authors |
| [Operations manual](docs/operations.md) | deploy, logs, troubleshooting, verification notes, test suite | operators |
| [Docker Hub blurb](docs/dockerhub-description.md) | bilingual text to paste into the Docker Hub page | image publishers |

Templates: [`config/webhook2nats.example.toml`](config/webhook2nats.example.toml) ·
[`relay.env.example`](relay.env.example)

## Development

```bash
python3 test_deploy.py        # docs ↔ config templates ↔ compose drift
cd relay
python3 test_internals.py     # config validation, normalization, @mentions, dedup, routing, privacy
python3 test_relay.py         # HTTP end-to-end: real sockets, real HMAC
python3 test_logs.py          # logging: degradation, privacy, rate limits, rotation
python3 test_e2e.py           # simulation: keep-alive / injection / concurrency / real NATS
```

**274 tests**, none of which need real NATS or internet (`test_e2e.py` spins up
its own container). See the [testing notes](docs/operations.md#测试).