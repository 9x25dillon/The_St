"""Composition root: every dependency the app and worker use, built once
from Settings and injectable piecewise in tests."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from app.auth import Authenticator, JwtBearerAuthenticator
from app.clock import Clock, SystemClock
from app.config import Settings
from app.consent.cascade import CascadeDispatcher, CeleryDispatcher, InlineDispatcher, run_cascade
from app.consent.kernel import KernelContext
from app.consent.tokens import KeyRing, SqlRevocationChecker, TokenIssuer, TokenValidator
from app.db import SessionFactory, make_engine, make_session_factory
from app.ratelimit import InMemoryGCRA, RateLimiter, RedisGCRA
from app.storage import LocalFileStorage, ObjectStorage, S3Storage, UrlSigner


@dataclass(frozen=True)
class Services:
    settings: Settings
    engine: Engine
    session_factory: SessionFactory
    clock: Clock
    keyring: KeyRing
    issuer: TokenIssuer
    validator: TokenValidator
    storage: ObjectStorage
    dispatcher: CascadeDispatcher
    rate_limiter: RateLimiter
    authenticator: Authenticator

    @property
    def kernel(self) -> KernelContext:
        return KernelContext(session_factory=self.session_factory, issuer=self.issuer, clock=self.clock)


def build_services(
    settings: Settings,
    *,
    engine: Engine | None = None,
    clock: Clock | None = None,
    keyring: KeyRing | None = None,
    storage: ObjectStorage | None = None,
    dispatcher: CascadeDispatcher | None = None,
    rate_limiter: RateLimiter | None = None,
    authenticator: Authenticator | None = None,
) -> Services:
    engine = engine or make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    clock = clock or SystemClock()
    keyring = keyring or KeyRing.from_settings(settings)
    issuer = TokenIssuer(
        keyring, issuer=settings.token_issuer, audience=settings.token_audience, ttl_seconds=settings.token_ttl_seconds
    )
    validator = TokenValidator(
        verification_keys=keyring.verification_keys,
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        revocation=SqlRevocationChecker(session_factory),
        clock=clock,
    )
    if storage is None:
        if settings.storage_backend == "s3":
            storage = S3Storage(settings.s3_bucket or "", region=settings.s3_region, endpoint_url=settings.s3_endpoint_url)
        else:
            secret = (
                settings.url_signing_secret.get_secret_value().encode()
                if settings.url_signing_secret
                else secrets.token_bytes(32)  # development: links die with the process
            )
            storage = LocalFileStorage(Path(settings.storage_local_root), UrlSigner(secret), settings.public_base_url)
    if dispatcher is None:
        if settings.cascade_dispatch == "inline":
            dispatcher = InlineDispatcher(
                lambda grant_id: run_cascade(grant_id, session_factory=session_factory, storage=storage, clock=clock)
            )
        else:
            dispatcher = CeleryDispatcher()
    if rate_limiter is None:
        if settings.rate_limit_backend == "redis":
            import redis

            rate_limiter = RedisGCRA(
                redis.Redis.from_url(settings.redis_url), settings.authorize_rate_per_minute, settings.authorize_burst
            )
        else:
            rate_limiter = InMemoryGCRA(settings.authorize_rate_per_minute, settings.authorize_burst)
    if authenticator is None:
        if settings.auth_jwt_key is None:
            raise ValueError("REMEMBRANCE_AUTH_JWT_KEY is required to authenticate callers")
        authenticator = JwtBearerAuthenticator(
            key=settings.auth_jwt_key.get_secret_value(),
            algorithm=settings.auth_jwt_algorithm,
            issuer=settings.auth_jwt_issuer,
            audience=settings.auth_jwt_audience,
        )
    return Services(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        clock=clock,
        keyring=keyring,
        issuer=issuer,
        validator=validator,
        storage=storage,
        dispatcher=dispatcher,
        rate_limiter=rate_limiter,
        authenticator=authenticator,
    )
