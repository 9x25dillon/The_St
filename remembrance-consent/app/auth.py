"""Inbound authentication seam.

This service does not own user accounts. The memorial web app (the
"existing auth") authenticates people and calls this service server-to-server
with a short-lived JWT identity assertion:

    sub            stable user id (required)
    email          the user's email
    email_verified must be true for `email` to be trusted (beneficiary matching)
    roles          list; "admin" grants the ADMIN role
    actor_kind     "human" (default) or "service"
    iss, aud, exp, iat

AuthMiddleware turns that assertion into an Actor on request.state.actor.
Swap JwtBearerAuthenticator for another Authenticator to integrate a
different identity provider; nothing else changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Protocol

import jwt
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import ApiError, error_response

MAX_SUBJECT_LENGTH = 255


class ActorKind(StrEnum):
    HUMAN = "HUMAN"
    SERVICE = "SERVICE"


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: str
    email: str | None = None  # present only when the identity provider verified it
    kind: ActorKind = ActorKind.HUMAN
    is_admin: bool = False


class Authenticator(Protocol):
    def authenticate(self, authorization_header: str | None) -> Actor | None: ...


class JwtBearerAuthenticator:
    def __init__(self, *, key: str, algorithm: str, issuer: str, audience: str, leeway_seconds: int = 10) -> None:
        self._key = key
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway_seconds

    def authenticate(self, authorization_header: str | None) -> Actor | None:
        if not authorization_header:
            return None
        scheme, _, token = authorization_header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        try:
            claims = jwt.decode(
                token.strip(),
                self._key,
                algorithms=[self._algorithm],  # pinned: no alg=none, no HS/RS confusion
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,
                options={"require": ["sub", "exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError:
            return None
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip() or len(subject) > MAX_SUBJECT_LENGTH:
            return None
        kind_claim = str(claims.get("actor_kind", "human")).lower()
        if kind_claim not in ("human", "service"):
            return None
        roles = claims.get("roles", [])
        if not isinstance(roles, list):
            return None
        email = claims.get("email")
        verified = claims.get("email_verified") is True and isinstance(email, str) and "@" in email
        return Actor(
            user_id=subject,
            email=email.strip().lower() if verified else None,
            kind=ActorKind.SERVICE if kind_claim == "service" else ActorKind.HUMAN,
            is_admin="admin" in {str(r).lower() for r in roles},
        )


class AuthMiddleware:
    """Pure ASGI middleware: authenticate, or answer 401 before any handler."""

    def __init__(self, app: ASGIApp, authenticator: Authenticator, exempt_prefixes: Iterable[str] = ()) -> None:
        self.app = app
        self.authenticator = authenticator
        self.exempt_prefixes = tuple(exempt_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(self.exempt_prefixes):
            await self.app(scope, receive, send)
            return
        actor = self.authenticator.authenticate(Headers(scope=scope).get("authorization"))
        if actor is None:
            response = error_response(
                ApiError(401, "UNAUTHENTICATED", "a valid bearer identity assertion is required"),
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["actor"] = actor
        await self.app(scope, receive, send)


def current_actor(request: Request) -> Actor:
    actor = getattr(request.state, "actor", None)
    if not isinstance(actor, Actor):
        raise ApiError(401, "UNAUTHENTICATED", "a valid bearer identity assertion is required")
    return actor
