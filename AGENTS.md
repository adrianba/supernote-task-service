# AGENTS.md

Guidance for AI agents and contributors working in this repository.

## What this is

A FastAPI service that exposes an authenticated REST API to manage Supernote
to-do tasks stored in the Supernote Private Cloud MariaDB (`supernotedb`,
tables `t_schedule_task` and `t_schedule_task_group`). See `README.md` for the
full feature and deployment overview.

## Tooling

- **Always use `uv`** to run Python — never call `pip` or `python` directly.
  - Install/sync deps: `uv sync`
  - Run the app: `uv run supernote-task-service`
  - One-off scripts: `uv run python -c "..."`
- Add dependencies with `uv add <pkg>` (and `uv add --dev <pkg>` for dev tools),
  then commit the updated `pyproject.toml` and `uv.lock`.

## Quality gates (run before committing)

```bash
uv run ruff check .            # lint
uv run ruff format --check .   # formatting
uv run mypy src                # strict type checking
uv run pytest                  # tests
```

All four must pass. `mypy` runs in strict mode; keep the code fully typed.

## CI & releases

- `.github/workflows/ci.yml` runs the quality gates above (via `uv`) plus a
  no-push Docker build on every push and PR.
- `.github/workflows/release.yml` is a manual `workflow_dispatch` that takes a
  semver `version`, bumps it in **both** `pyproject.toml` and
  `src/supernote_task_service/__init__.py` (keep these two in sync — the value
  feeds the FastAPI app `version`), runs tests, publishes the image to GHCR
  (`:<version>` and `:latest`), commits the bump, tags `v<version>`, and creates
  a GitHub release.
- Workflows pin the latest major action versions (checkout v7, setup-uv
  v8.2.0 — that action publishes no moving major tag, so pin the full
  version; docker/* login v4 / metadata v6 / setup-buildx v4 / build-push v7).
  Bump to the newest majors when updating to avoid deprecated-runtime warnings.

## Project layout

```
src/supernote_task_service/
  config.py       # env-driven Pydantic Settings; API-key hashing helper
  security.py     # API-key auth dependency (constant-time, hashed)
  ratelimit.py    # in-memory fixed-window rate limiter
  db.py           # pymysql connection pool + single-user user_id detection
  encoding.py     # ms timestamps, emoji [U+XXXX], document links, ID validation
  models.py       # Pydantic request/response models
  repository.py   # parameterized SQL data-access (tasks + lists, delta sync)
  deps.py         # FastAPI dependencies (settings, repo, rate limit)
  errors.py       # exception handlers (DB → 503, unexpected → 500)
  routers/        # health, lists, tasks
  main.py         # app factory, middleware, lifespan
  __main__.py     # uvicorn entry point (`app` ASGI object + `main()`)
tests/            # pytest; FakeRepository in conftest.py (no real DB needed)
```

## Supernote database rules (do not violate)

These come from https://github.com/adrianba/supernote-todo and are enforced in
`repository.py` / `encoding.py`:

- **Timestamps are Unix milliseconds.** Use `encoding.now_ms()` / the datetime
  helpers; never store seconds.
- **Soft delete only.** Set `is_deleted='Y'`; never issue SQL `DELETE`.
- **Always set `last_modified` to now on every write** so the device treats the
  change as newest (last-write-wins).
- **Emoji must be encoded** as `[U+XXXX]` on write and decoded on read; the
  `detail` column is `varchar(255)` and is truncated *after* encoding.
- **Preserve the `links` (document link) column** on updates unless the caller
  explicitly changes `document_link`.
- **IDs are 32-char lowercase hex** (`uuid.uuid4().hex`); validate inputs.
- **Inbox is implicit:** `task_list_id IS NULL` (no group row).
- **Single-user:** `user_id` is auto-detected and cached in `db.Database`.

## Incremental sync

`since`-based queries use a closed window `since <= last_modified <= cursor`
(inclusive lower bound so no same-millisecond change is ever missed) and return
`cursor` for the next call. Results are capped by `limit` (default 500, max
1000); a full page advances the cursor only to the last delivered row so callers
can page through. Responses also carry `has_more` (true while the page was
capped at the limit). Delta responses include completed and soft-deleted rows so
clients can propagate deletions, and clients must treat sync as idempotent.
Don't change these semantics without updating both `repository.py` and the
README. Optional `CURSOR_MAX_AGE_MS` (default `0` = off) makes a too-old `since`
return `410 Gone` (`code: "cursor_expired"`); keep it defaulted off.

## API conventions

- `POST /v1/tasks` and `POST /v1/lists` return the **full created resource**
  (`201`); list creation is **idempotent by encoded title** (existing
  non-deleted match returns `200`).
- Mutating endpoints accept an optional `If-Unmodified-Since` header (the
  client's last-known `last_modified` as **Unix ms**); a version mismatch is
  `409 Conflict` while a missing row stays `404`. Absent header = unconditional
  last-write-wins. Thread `expected_last_modified` through `repository.py`.
- `importance` (task priority 1–5 / null) maps to the varchar
  `t_schedule_task.importance` column (stored as a numeric string).
- Error bodies are always `{"detail", "code"}`; raise `ApiError` for a specific
  `code`, otherwise the status→code map in `errors.py` applies.
- `due` accepts date-only `YYYY-MM-DD` (midnight UTC); naive datetimes are UTC.
  There is **no task start-date column** in the schema — do not invent one.

## Security conventions

- All SQL must be parameterized (`%s` placeholders). The only interpolated parts
  are constant column lists / pre-built clause fragments (annotated with
  `# noqa: S608`); never interpolate caller-supplied values.
- Never log API keys, passwords, or full request bodies.
- New endpoints under `/v1` must depend on `deps.CallerDep` (auth + rate limit).

## Coding style

- Comment only where intent isn't obvious; avoid restating the code.
- Keep handlers thin; put data access in `repository.py`.
- pymysql is blocking — keep route handlers as sync `def` so FastAPI runs them
  in its threadpool.
