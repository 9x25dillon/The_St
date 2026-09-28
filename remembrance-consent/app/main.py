"""Application factory.

Request path, outermost first:

    AuthMiddleware        401 unless the caller presents a valid identity
    RateLimitMiddleware   429 on POST /consent/authorize beyond the budget
    PurposeGuardMiddleware  403 + audit on banned purposes
    routers               consent, successors, profiles, export

Run:  uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8790
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth import AuthMiddleware
from app.config import Settings
from app.consent.kernel import PurposeViolationError
from app.consent.middleware import PurposeGuardMiddleware
from app.consent.router import router as consent_router
from app.db import create_sqlite_schema
from app.errors import ApiError, error_response
from app.export.router import router as export_router
from app.profiles.router import router as profiles_router
from app.ratelimit import RateLimitMiddleware
from app.services import Services, build_services
from app.successors.router import router as successors_router

log = logging.getLogger(__name__)

UNAUTHENTICATED_PREFIXES = ("/healthz", "/export/download", "/.well-known/")
DEVELOPMENT_DOC_PREFIXES = ("/docs", "/openapi.json")
RATE_LIMITED_PATHS = ("/consent/authorize",)


def create_app(services: Services | None = None) -> FastAPI:
    services = services or build_services(Settings())
    production = services.settings.env == "production"
    if services.engine.dialect.name == "sqlite":
        # Development convenience; PostgreSQL schemas come only from Alembic.
        create_sqlite_schema(services.engine)
    app = FastAPI(
        title="Remembrance Consent Kernel",
        version="1.0.0",
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )
    app.state.services = services
    app.state.token_validator = services.validator

    app.include_router(consent_router)
    app.include_router(successors_router)
    app.include_router(profiles_router)
    app.include_router(export_router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        with services.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/.well-known/consent-keys", tags=["tokens"])
    def consent_keys() -> dict[str, object]:
        """Public keys downstream services use to verify kernel tokens."""
        return {
            "algorithm": "EdDSA",
            "issuer": services.settings.token_issuer,
            "audience": services.settings.token_audience,
            "keys": services.keyring.public_keys_pem(),
        }

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(exc)

    @app.exception_handler(PurposeViolationError)
    async def _purpose_violation(_: Request, exc: PurposeViolationError):
        return error_response(ApiError(403, "PURPOSE_VIOLATION", str(exc), audit_event_id=exc.audit_event_id))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        # Never echo submitted values back: they may carry personal data.
        problems = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
        return error_response(ApiError(422, "VALIDATION_FAILED", "request validation failed", problems=problems))

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError):
        log.warning("integrity error: %s", exc.orig)
        return error_response(ApiError(409, "CONFLICT", "the request conflicts with the current state"))

    # add_middleware wraps: the last one added is the outermost.
    app.add_middleware(PurposeGuardMiddleware, services_getter=lambda: app.state.services)
    app.add_middleware(RateLimitMiddleware, limiter=services.rate_limiter, paths=RATE_LIMITED_PATHS)
    exempt = UNAUTHENTICATED_PREFIXES + (() if production else DEVELOPMENT_DOC_PREFIXES)
    app.add_middleware(AuthMiddleware, authenticator=services.authenticator, exempt_prefixes=exempt)
    return app
