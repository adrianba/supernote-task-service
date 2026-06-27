# Supernote Task Service

An authenticated HTTP API for synchronizing **Supernote to-do tasks** with the
Supernote Private Cloud's MariaDB database. It exposes a clean REST interface to
add, update, complete, and remove tasks across lists/categories, and supports
**incremental sync** so a client can fetch only what changed since its last call.

The service is designed to run in a Docker container on the **same Docker
network** as the Supernote MariaDB container, behind a TLS-terminating reverse
proxy (Traefik).

> The underlying database schema and access patterns are documented in
> [adrianba/supernote-todo](https://github.com/adrianba/supernote-todo). All
> writes follow that guide's safety rules (soft deletes, millisecond timestamps,
> emoji encoding, document-link preservation).

---

## Architecture

```
        HTTPS                       Docker network: proxy
client ───────► Traefik (TLS) ──────────────────────────┐
                                                        ▼
                                          supernote-task-service (FastAPI)
                                                        │  Docker network: supernote
                                                        ▼
                                                 supernote-mariadb
                                                   (supernotedb)
```

- **Auth:** every `/v1/*` request requires a valid API key.
- **Rate limiting:** in-memory fixed window per API key + client IP.
- **No host ports:** Traefik reaches the app internally; the app reaches MariaDB
  internally. Nothing is published directly to the host.

---

## Requirements

- [uv](https://docs.astral.sh/uv/) for local development.
- Docker for container builds and deployment.
- Network reachability to the Supernote MariaDB container.

---

## Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)).

| Variable                       | Default             | Description                                                            |
| ------------------------------ | ------------------- | ---------------------------------------------------------------------- |
| `SUPERNOTE_DB_PASSWORD`        | **(required)**      | MariaDB password for the `supernote` user.                             |
| `SUPERNOTE_DB_HOST`            | `supernote-mariadb` | MariaDB hostname (container name on the shared network).               |
| `SUPERNOTE_DB_PORT`            | `3306`              | MariaDB port.                                                          |
| `SUPERNOTE_DB_USER`            | `supernote`         | MariaDB user.                                                          |
| `SUPERNOTE_DB_NAME`            | `supernotedb`       | Database name.                                                         |
| `SUPERNOTE_USER_EMAIL`         | `""`                | Email of the single Supernote user to expose (looked up in `u_user`). Required when the database has more than one user; auto-detected when there is exactly one. |
| `SUPERNOTE_DB_CONNECT_TIMEOUT` | `10`                | Connect/read/write timeout (seconds).                                  |
| `SUPERNOTE_DB_POOL_SIZE`       | `5`                 | Max pooled connections.                                                |
| `API_KEYS`                     | `""`                | Comma-separated API keys. Hashed in memory; compared in constant time. |
| `RATE_LIMIT_REQUESTS`          | `120`               | Allowed requests per window per key + IP.                              |
| `RATE_LIMIT_WINDOW_SECONDS`    | `60`                | Rate-limit window length (seconds).                                    |
| `UNAUTH_RATE_LIMIT_REQUESTS`   | `30`                | Pre-auth requests per window per IP (throttles key guessing).          |
| `LOG_LEVEL`                    | `INFO`              | Logging level.                                                         |
| `ENABLE_DOCS`                  | `false`             | Expose Swagger UI at `/docs` and `/openapi.json`.                      |
| `MAX_REQUEST_BODY_BYTES`       | `65536`             | Reject bodies larger than this.                                        |
| `CURSOR_MAX_AGE_MS`            | `0`                 | Max age (ms) of a delta `since` cursor before `410 Gone`; `0` = off.   |
| `TRUST_PROXY_HEADERS`          | `true`              | Trust `X-Forwarded-*`. Enable only behind a trusted proxy.             |

> **Generate an API key:** `python -c "import secrets; print(secrets.token_urlsafe(32))"`
>
> If `API_KEYS` is empty the service **fails closed** and rejects all
> authenticated requests.

### Multi-user databases

The service exposes tasks for **exactly one** Supernote user; every query is
scoped to that user's `user_id`, so other users' tasks and lists are never read
or modified.

- **Single-user database (default):** the user is auto-detected, so no extra
  configuration is needed.
- **Multiple users:** set `SUPERNOTE_USER_EMAIL` to the `email` of the user to
  expose (matched against the `u_user` table). If more than one user exists and
  `SUPERNOTE_USER_EMAIL` is unset — or it does not match any user — the service
  **fails fast at startup** with a clear error rather than guessing.

A transient database outage at startup is non-fatal: resolution is retried
lazily on the first request that needs it.

---

## Running locally

```bash
uv sync
export SUPERNOTE_DB_PASSWORD=...        # or use a .env file
export API_KEYS=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export ENABLE_DOCS=true                 # optional, for /docs
uv run supernote-task-service
```

The service listens on `http://0.0.0.0:8000` (override with `HOST`/`PORT`).

### Quality gates

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy src              # type checking
uv run pytest                # tests
```

---

## Docker & Traefik deployment

Build and run via the provided [`docker-compose.yml`](docker-compose.yml), which
assumes an existing Traefik instance on an external `proxy` network and the
Supernote MariaDB on an external `supernote` network:

```bash
cp .env.example .env          # then edit secrets
docker compose up -d --build
```

Adjust the `Host(...)` rule, the `certresolver`, and the network names to match
your environment. The image is Alpine-based, runs as a non-root user, and ships
with a container `HEALTHCHECK` hitting `/healthz`.

---

## API

Base path: `/v1`. All endpoints require an API key via either header:

```
Authorization: Bearer <API_KEY>
X-API-Key: <API_KEY>
```

Health and version endpoints are unauthenticated: `GET /healthz` (liveness),
`GET /readyz` (checks DB connectivity), and `GET /v1/version` (returns
`{"version": "..."}`).

### Lists (categories)

| Method   | Path             | Description                                                                    |
| -------- | ---------------- | ------------------------------------------------------------------------------ |
| `GET`    | `/v1/lists`      | List categories (plus the implicit Inbox). Supports `?since=`, `?title=`.      |
| `POST`   | `/v1/lists`      | Create a category (**idempotent by title**). Body: `{"title": "Work"}`.        |
| `PATCH`  | `/v1/lists/{id}` | Rename a category.                                                             |
| `DELETE` | `/v1/lists/{id}` | Soft-delete a category.                                                        |

`POST /v1/lists` is **idempotent by title**: if a non-deleted list with the same
(emoji-encoded) title already exists it is returned with `200 OK` instead of
creating a duplicate; a freshly created list returns `201 Created`. This removes
the read-modify-write race in "ensure a list by name". Soft-deleted lists with
the same title do **not** block creation. `GET /v1/lists?title=<exact>` returns a
single-element page for an exact title match, or `404` if none exists.

### Tasks

| Method   | Path                        | Description                                                                          |
| -------- | --------------------------- | ------------------------------------------------------------------------------------ |
| `GET`    | `/v1/tasks`                 | List/sync tasks. Filters: `since`, `list_id`, `inbox`, `include_completed`, `limit`. |
| `POST`   | `/v1/tasks`                 | Create a task. Returns the full created task (`201`).                                |
| `GET`    | `/v1/tasks/{id}`            | Get a task.                                                                          |
| `PATCH`  | `/v1/tasks/{id}`            | Partial update (omitted fields unchanged).                                           |
| `DELETE` | `/v1/tasks/{id}`            | Soft-delete a task.                                                                  |
| `POST`   | `/v1/tasks/{id}/complete`   | Mark completed.                                                                      |
| `POST`   | `/v1/tasks/{id}/uncomplete` | Mark active.                                                                         |

Task and list IDs are 32-character lowercase hex strings. `POST /v1/tasks` and
`POST /v1/lists` return the full created resource (including `id` and
`last_modified`), so a client need not issue a follow-up `GET`.

#### Create a task

```bash
curl -X POST https://tasks.example.com/v1/tasks \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title": "Buy groceries 🛒", "detail": "milk, eggs", "importance": 2, "due": "2026-05-01T09:00:00Z"}'
# → 201 Created
# {
#   "id": "a1b2c3...", "list_id": null, "category": "Inbox",
#   "title": "Buy groceries 🛒", "detail": "milk, eggs",
#   "status": "needsAction", "importance": 2,
#   "due": "2026-05-01T09:00:00Z", "completed": null,
#   "last_modified": 1714560001234, "is_deleted": false
# }
```

A task body accepts:

```jsonc
{
  "title": "string (1..600)",
  "detail": "string (<=255)",
  "list_id": "32-hex or null (null = Inbox)",
  "status": "needsAction | completed",
  "importance": "integer 1 (highest)..5, or null for none",
  "due": "RFC3339 datetime, a date-only YYYY-MM-DD, or null",
  "document_link": {
    "appName": "note",
    "fileId": "...",
    "filePath": "...",
    "page": 3,
    "pageId": "...",
  },
}
```

#### Date / timezone semantics

- `due` (and the read-only `completed`) are stored as Unix milliseconds.
- A **date-only** string such as `"2026-06-25"` is interpreted as **midnight
  UTC**, so a pure date round-trips without drifting a day.
- A **naive** datetime (no offset) is interpreted as **UTC**. Prefer sending
  RFC3339 with an explicit offset (e.g. `2026-05-01T09:00:00+02:00`) to avoid
  ambiguity.

#### Optimistic concurrency

The mutating task and list endpoints (`PATCH`, `DELETE`,
`POST .../complete`, `POST .../uncomplete`) accept an optional
**`If-Unmodified-Since`** request header carrying the client's last-known
`last_modified` as a **Unix millisecond integer** (not an HTTP-date):

```
If-Unmodified-Since: 1714560001234
```

- If the stored row still has that `last_modified`, the write proceeds.
- If the row was modified since (e.g. a concurrent edit on the Supernote
  device), the API returns **`409 Conflict`** (`code: "conflict"`) and does not
  write. The client should re-read and retry.
- If the row does not exist, the API returns `404` as usual.
- When the header is **absent**, behavior is unchanged: an unconditional,
  last-write-wins update.

### Incremental sync

Both `GET /v1/tasks` and `GET /v1/lists` accept `?since=<ms>` (a Unix
millisecond cursor). The response includes a `cursor` to pass on the next call
and a `has_more` flag:

```jsonc
// GET /v1/tasks?since=1714560000000
{
  "tasks": [
    {
      "id": "…",
      "title": "…",
      "status": "needsAction",
      "is_deleted": false,
      "last_modified": 1714560001234,
    },
    { "id": "…", "title": "…", "is_deleted": true, "last_modified": 1714560005000 },
  ],
  "cursor": 1714560006000,
  "has_more": false,
}
```

Contract a client can rely on:

- A call with `since` returns every row changed in the closed window
  `since <= last_modified <= cursor`, **including completed and soft-deleted
  rows** so clients can propagate deletions (`"is_deleted": true`).
- The lower bound is **inclusive**, so a change written at exactly the previous
  cursor millisecond is never missed; clients must therefore treat sync as
  **idempotent** — a boundary row may occasionally be re-delivered and
  re-applying it is a no-op.
- Rows are returned in a stable order: **`last_modified ASC, task_id ASC`**
  (lists order by `last_modified ASC, task_list_id ASC`).
- Results are capped (`limit`, default 500, max 1000). When a full page is
  returned the `cursor` advances only to the last delivered row's timestamp.
- **`has_more`** is `true` exactly when the page was capped at the effective
  limit and more rows may remain. **Keep calling while `has_more` is `true`**,
  passing the returned `cursor` as the next `since`. It becomes `false` once the
  tail is reached.
- Omitting `since` returns the current active set (non-deleted), and the
  `cursor` can be used to begin incremental syncing afterwards.

> Sync uses the device's own `last_modified` (Unix ms) column, so it is
> last-write-wins relative to the Supernote device. All writes set
> `last_modified` to the current time.

#### Expired cursors (410)

Cursor expiry is **disabled by default** (`CURSOR_MAX_AGE_MS=0`), so every old
`since` value keeps working. If `CURSOR_MAX_AGE_MS` is set to a positive number
of milliseconds and a request supplies a `since` older than that retention
bound, the API returns **`410 Gone`** (`code: "cursor_expired"`) to signal the
client to drop its cursor and perform a full resync (a request without `since`).

### Errors

JSON error bodies are always `{"detail": "...", "code": "..."}`. The `code` is a
stable, machine-readable string; `detail` is human-readable prose.

| Status | `code`             | Meaning                                       |
| ------ | ------------------ | --------------------------------------------- |
| `401`  | `unauthorized`     | Missing/invalid API key.                      |
| `404`  | `not_found`        | Task or list not found.                       |
| `409`  | `conflict`         | Stale `If-Unmodified-Since` precondition.     |
| `410`  | `cursor_expired`   | `since` older than the retention bound.       |
| `413`  | `payload_too_large`| Request body too large.                       |
| `422`  | `validation_error` | Validation error (bad field or malformed ID). |
| `429`  | `rate_limited`     | Rate limit exceeded (includes `Retry-After`). |
| `503`  | `db_unavailable`   | Database unavailable.                         |

Successful authenticated responses include `X-RateLimit-Limit`,
`X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers.

### Limitations

- **Task start date is not supported.** The Supernote `t_schedule_task` schema
  has no start/begin column, so a task start date cannot be stored and does not
  round-trip. Only `due` (and the read-only `completed`) timestamps are
  available.

---

## Security notes

- API keys are never logged and are stored only as SHA-256 hashes in memory;
  comparison is constant time.
- All SQL uses parameterized queries; path/query IDs are validated as 32-hex.
- Soft deletes only — rows are marked `is_deleted='Y'`, never hard-deleted,
  to stay compatible with Supernote device sync.
- The container runs as a non-root user with `no-new-privileges`, a read-only
  root filesystem, and all Linux capabilities dropped (see compose file).
- Interactive docs are disabled by default (`ENABLE_DOCS=false`).

---

## Continuous integration & releases

Two GitHub Actions workflows live in [`.github/workflows`](.github/workflows):

- **CI** (`ci.yml`) runs on every push and pull request. It lints, checks
  formatting, type-checks, and runs the test suite with `uv`, and separately
  builds the Docker image (without pushing) so a broken `Dockerfile` fails CI.
- **Release** (`release.yml`) is triggered manually via *Run workflow* and
  prompts for a semver **version** (e.g. `1.2.3`, no leading `v`). It bumps the
  version in both `pyproject.toml` and `src/supernote_task_service/__init__.py`,
  runs the tests, builds and pushes the image to GHCR
  (`ghcr.io/<owner>/supernote-task-service`) tagged with the version and
  `latest`, commits the bump, creates a `v<version>` git tag, and opens a
  GitHub release.

Pull the published image with:

```bash
docker pull ghcr.io/<owner>/supernote-task-service:<version>
```

---

## License

MIT — see [LICENSE](LICENSE).
