"""Application configuration loaded from environment variables."""

from __future__ import annotations

import hashlib

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _hash_key(raw: str) -> str:
    """Return the lowercase hex SHA-256 of an API key."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Settings(BaseSettings):
    """Runtime configuration.

    Values are read from environment variables (and an optional ``.env`` file).
    Secrets are never logged.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database connection (same Docker network: connect by container name).
    db_host: str = Field(default="supernote-mariadb", alias="SUPERNOTE_DB_HOST")
    db_port: int = Field(default=3306, alias="SUPERNOTE_DB_PORT")
    db_user: str = Field(default="supernote", alias="SUPERNOTE_DB_USER")
    db_password: str = Field(alias="SUPERNOTE_DB_PASSWORD")
    db_name: str = Field(default="supernotedb", alias="SUPERNOTE_DB_NAME")
    db_connect_timeout: int = Field(default=10, alias="SUPERNOTE_DB_CONNECT_TIMEOUT")
    db_pool_size: int = Field(default=5, alias="SUPERNOTE_DB_POOL_SIZE")

    # Authentication: comma-separated API keys. Stored hashed in memory.
    api_keys: str = Field(default="", alias="API_KEYS")

    # Rate limiting (fixed window per credential + client IP).
    rate_limit_requests: int = Field(default=120, alias="RATE_LIMIT_REQUESTS")
    rate_limit_window_seconds: int = Field(default=60, alias="RATE_LIMIT_WINDOW_SECONDS")

    # Operational.
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    enable_docs: bool = Field(default=False, alias="ENABLE_DOCS")
    max_request_body_bytes: int = Field(default=65536, alias="MAX_REQUEST_BODY_BYTES")
    # Trust the proxy-supplied client IP header (set only behind a trusted proxy).
    trust_proxy_headers: bool = Field(default=True, alias="TRUST_PROXY_HEADERS")

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @property
    def api_key_hashes(self) -> frozenset[str]:
        """Return the set of SHA-256 hashes for all configured API keys."""
        keys = [k.strip() for k in self.api_keys.split(",") if k.strip()]
        return frozenset(_hash_key(k) for k in keys)

    def has_api_keys(self) -> bool:
        return bool(self.api_key_hashes)
