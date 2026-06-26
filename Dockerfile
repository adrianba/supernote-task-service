# syntax=docker/dockerfile:1

# ---- Builder: resolve and install dependencies into a virtual environment ----
FROM python:3.12-alpine AS builder

# Copy the uv binary from its official, pinned image.
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install only third-party dependencies first for better layer caching.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable

# Install the project itself.
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- Runtime: minimal image running as a non-root user ----
FROM python:3.12-alpine AS runtime

# Create an unprivileged user to run the service.
RUN addgroup -S app && adduser -S -G app -h /app app

WORKDIR /app

# Copy the prepared virtual environment from the builder stage.
COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000

USER app
EXPOSE 8000

# Lightweight liveness check (no extra tooling required).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"]

CMD ["supernote-task-service"]
