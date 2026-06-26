"""Console entry point for running the service with Uvicorn."""

from __future__ import annotations

import os

import uvicorn

from .config import Settings
from .main import create_app

# ASGI application for `uvicorn supernote_task_service.__main__:app`.
app = create_app()


def main() -> None:
    settings = Settings()
    host = os.environ.get("HOST", "0.0.0.0")  # noqa: S104  # bound inside container network
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=settings.log_level.lower(),
        proxy_headers=settings.trust_proxy_headers,
        forwarded_allow_ips="*" if settings.trust_proxy_headers else None,
    )


if __name__ == "__main__":
    main()
