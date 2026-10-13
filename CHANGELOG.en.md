# Changelog

**English · [中文版](CHANGELOG.md)**

> This is a translated summary for convenience. The
> [Chinese changelog](CHANGELOG.md) is canonical — if the two ever disagree,
> the Chinese one is correct.

This project follows [semantic versioning](https://semver.org/).

This file records **user-facing** changes. The **single source of truth for the
version** is the [`VERSION`](./VERSION) file at the repo root — CI injects it
into the build, and a test asserts that the top entry here matches it. Version
numbers are deliberately not hardcoded anywhere else.

---

## 1.0.0

First stable release.

### Breaking

- **Response bodies changed from plain text to JSON.** `POST` responses used to
  be bare strings like `accepted` / `duplicate`; they are now JSON:

  ```json
  {"status": "accepted", "delivery": "<uuid>", "subjects": ["evt.gitea.notify.h1.alice"]}
  ```

  `Content-Type` is now `application/json; charset=utf-8`.

  **Migration**: any check like `if body == "accepted"` **fails silently** — you
  now receive a dict instead of a string, so the comparison is always false with
  no error. Parse the fields instead:

  ```python
  doc = response.json()
  ok = doc.get("status") == "accepted"
  # on failure: doc.get("error"), and usually doc.get("hint") saying
  # which piece of configuration to change
  ```

  `GET /healthz` is affected too: `ok` → `{"status": "ok"}`,
  `nats-down` → `{"error": "nats-down"}`.

### Added

- **`GET /version`** — returns the release version (`version`), the exact commit
  (`commit`), the start time, and uptime. For answering "which build am I
  running?"
- **`[logging]` config section** — structured logging, one line per request by
  default, carrying `delivery` / `repo#number` / `mentions` / `subjects` /
  `status` / `dur_ms`, joinable with NATS messages by `delivery`. The whole
  section is optional; omitting it means stdout-only, info level, text format,
  no bodies. See the [operations manual](docs/operations.md).
- **Subject prefix startup validation** — the build now **fails fast** when
  `notify_subject_prefix` or `broadcast_subject` contains NATS wildcards, or
  when the broadcast subject sits under the notify prefix. Both of those misroute
  bells with no error at all.
- **Stricter config validation** — wrong types, zero values, and unknown keys
  refuse to start instead of silently falling back to defaults.

### Fixed

- **Dedup state file type validation** — an explicit error when the file is valid
  JSON but the wrong shape, instead of silently rebuilding empty state.
- **Per-entry member validation** — one non-dict entry rejects the whole file
  and names the offending login, instead of every webhook raising.
- **Publish timeout budget** — the NATS-side budget is now lower than the HTTP
  layer's, so NATS gives up before HTTP does, narrowing the duplicate-delivery
  window.
- **`@mention` parsing** — adjacent CJK characters now match; `@` inside code
  blocks, inline code, emails, and paths no longer triggers a notification.

### Known limits

- **Not exactly-once.** Core NATS has no acks, so the timeout window cannot be
  eliminated. Dedup guarantees "not processed twice"; agents needing strict
  idempotency should dedupe on `delivery` themselves.
- **Bells are lost while offline.** That is Core NATS by design; a separate
  low-frequency sweep reconciles against the source platform's unread state.
- **One relay instance per webhook callback.** Dedup state is local — multiple
  instances behind one callback duplicate deliveries with no error. See the
  [operations manual](docs/operations.md#部署).
