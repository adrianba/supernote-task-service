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
can page through. Delta responses include completed and soft-deleted rows so
clients can propagate deletions, and clients must treat sync as idempotent.
Don't change these semantics without updating both `repository.py` and the
README.

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
