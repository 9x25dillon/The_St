"""Service configuration. Every key is read from the environment with the
REMEMBRANCE_ prefix; see .env.example for the full list.

Production refuses to start with development fallbacks (ephemeral keys,
SQLite, inline cascade), so a misconfigured deploy fails loudly instead of
silently issuing tokens nobody downstream can verify.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hard ceiling on authorization token lifetime, independent of configuration.
MAX_TOKEN_TTL_SECONDS = 300
MIN_SECRET_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REMEMBRANCE_", extra="ignore", frozen=True)

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite+pysqlite:///./var/remembrance.db"
    migration_database_url: str | None = None

    token_signing_key_pem: SecretStr | None = None
    token_key_id: str = "kernel-dev"
    token_verification_keys_json: str = "{}"
    token_issuer: str = "remembrance-consent-kernel"
    token_audience: str = "remembrance-downstream"
    token_ttl_seconds: int = Field(MAX_TOKEN_TTL_SECONDS, ge=1, le=MAX_TOKEN_TTL_SECONDS)

    auth_jwt_algorithm: Literal["HS256", "HS384", "HS512", "RS256", "ES256", "EdDSA"] = "HS256"
    auth_jwt_key: SecretStr | None = None
    auth_jwt_issuer: str = "remembrance-web"
    auth_jwt_audience: str = "remembrance-consent"

    authorize_rate_per_minute: int = Field(30, ge=1)
    authorize_burst: int = Field(10, ge=1)
    rate_limit_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://127.0.0.1:6379/1"

    storage_backend: Literal["local", "s3"] = "local"
    storage_local_root: str = "./var/objects"
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_endpoint_url: str | None = None
    public_base_url: str = "http://127.0.0.1:8790"
    url_signing_secret: SecretStr | None = None
    export_url_ttl_seconds: int = Field(900, ge=60, le=86_400)

    celery_broker_url: str = "redis://127.0.0.1:6379/0"
    cascade_dispatch: Literal["celery", "inline"] = "celery"
    cascade_stale_after_seconds: int = Field(600, ge=30)

    @property
    def verification_keys(self) -> dict[str, str]:
        raw = json.loads(self.token_verification_keys_json or "{}")
        if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
            raise ValueError("REMEMBRANCE_TOKEN_VERIFICATION_KEYS_JSON must be an object of {kid: public PEM}")
        return raw

    @model_validator(mode="after")
    def _production_requires_real_material(self) -> "Settings":
        _ = self.verification_keys  # validate the JSON shape eagerly
        if self.env != "production":
            return self
        problems = []
        if self.token_signing_key_pem is None:
            problems.append("TOKEN_SIGNING_KEY_PEM is required")
        if self.auth_jwt_key is None:
            problems.append("AUTH_JWT_KEY is required")
        elif self.auth_jwt_algorithm.startswith("HS") and len(self.auth_jwt_key.get_secret_value()) < MIN_SECRET_BYTES:
            problems.append(f"AUTH_JWT_KEY must be at least {MIN_SECRET_BYTES} bytes for HMAC")
        if self.storage_backend == "local" and (
            self.url_signing_secret is None or len(self.url_signing_secret.get_secret_value()) < MIN_SECRET_BYTES
        ):
            problems.append(f"URL_SIGNING_SECRET of at least {MIN_SECRET_BYTES} bytes is required for local storage")
        if self.storage_backend == "s3" and not self.s3_bucket:
            problems.append("S3_BUCKET is required for s3 storage")
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL must be PostgreSQL in production (append-only guarantees need role grants)")
        if self.cascade_dispatch == "inline":
            problems.append("CASCADE_DISPATCH=inline is for development only")
        if problems:
            raise ValueError("production configuration invalid: " + "; ".join(problems))
        return self
