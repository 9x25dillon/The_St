"""Authorization tokens: the only thing downstream features may trust.

The kernel signs with an Ed25519 private key (JWT alg EdDSA). Downstream
services hold public keys only, so a compromised downstream service can
verify tokens but never mint them. Every token:

  - lives at most 300 s (MAX_TOKEN_TTL_SECONDS, enforced at issue *and* at
    validation, so a misconfigured issuer can't mint long-lived tokens);
  - names exactly one action and one profile;
  - has a jti recorded in authorization_token. Validation fails closed: a
    jti the ledger doesn't know is rejected, and a revoked one is rejected.

Downstream services validate with TokenValidator (or the FastAPI dependency
`require_consent`) and read nothing but authorization_token's revocation
columns; the database role remembrance_token_reader enforces that.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import Request
from sqlalchemy import select

from app.clock import Clock
from app.config import MAX_TOKEN_TTL_SECONDS, Settings
from app.consent.constants import SYNTHESIS_ACTIONS, ConsentAction
from app.consent.models import AuthorizationToken
from app.db import SessionFactory
from app.errors import ApiError

log = logging.getLogger(__name__)

TOKEN_TYPE = "consent+jwt"
ALGORITHM = "EdDSA"
CLOCK_SKEW_SECONDS = 5
CONSENT_TOKEN_HEADER = "X-Consent-Authorization"
REQUIRED_CLAIMS = ("iss", "aud", "sub", "jti", "iat", "nbf", "exp", "act", "pid", "pur")


def _load_private(pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("token signing key must be an Ed25519 private key")
    return key


def _load_public(pem: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("token verification keys must be Ed25519 public keys")
    return key


def public_pem(key: Ed25519PublicKey) -> str:
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def private_pem(key: Ed25519PrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@dataclass(frozen=True)
class KeyRing:
    signing_key: Ed25519PrivateKey
    signing_kid: str
    verification_keys: Mapping[str, Ed25519PublicKey]

    @classmethod
    def generate(cls, kid: str = "kernel-ephemeral") -> "KeyRing":
        key = Ed25519PrivateKey.generate()
        return cls(signing_key=key, signing_kid=kid, verification_keys={kid: key.public_key()})

    @classmethod
    def from_settings(cls, settings: Settings) -> "KeyRing":
        extra = {kid: _load_public(pem) for kid, pem in settings.verification_keys.items()}
        if settings.token_signing_key_pem is None:
            if settings.env == "production":  # pragma: no cover - Settings already rejects this
                raise ValueError("production requires REMEMBRANCE_TOKEN_SIGNING_KEY_PEM")
            log.warning("no token signing key configured; using an ephemeral key (tokens die with this process)")
            ring = cls.generate(settings.token_key_id)
            return cls(ring.signing_key, ring.signing_kid, {**extra, **ring.verification_keys})
        key = _load_private(settings.token_signing_key_pem.get_secret_value())
        return cls(key, settings.token_key_id, {**extra, settings.token_key_id: key.public_key()})

    def public_keys_pem(self) -> dict[str, str]:
        return {kid: public_pem(key) for kid, key in sorted(self.verification_keys.items())}


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: UUID
    issued_at: datetime
    expires_at: datetime


class TokenIssuer:
    def __init__(self, keyring: KeyRing, *, issuer: str, audience: str, ttl_seconds: int = MAX_TOKEN_TTL_SECONDS) -> None:
        if not 1 <= ttl_seconds <= MAX_TOKEN_TTL_SECONDS:
            raise ValueError(f"token TTL must be within 1..{MAX_TOKEN_TTL_SECONDS} seconds")
        self._keyring = keyring
        self._issuer = issuer
        self._audience = audience
        self._ttl = ttl_seconds

    def issue(
        self,
        *,
        jti: UUID,
        actor_id: str,
        action: ConsentAction,
        profile_id: UUID,
        grant_id: UUID | None,
        purpose: str,
        now: datetime,
    ) -> IssuedToken:
        issued_at = now.astimezone(UTC).replace(microsecond=0)
        expires_at = issued_at + timedelta(seconds=self._ttl)
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": actor_id,
            "jti": str(jti),
            "iat": int(issued_at.timestamp()),
            "nbf": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "act": action.value,
            "pid": str(profile_id),
            "gid": str(grant_id) if grant_id else None,
            "pur": purpose,
            # Anything synthesized under this token must be presented as
            # synthetic; the deceased never speaks in the first person.
            "dsc": action in SYNTHESIS_ACTIONS,
        }
        token = jwt.encode(
            claims,
            self._keyring.signing_key,
            algorithm=ALGORITHM,
            headers={"kid": self._keyring.signing_kid, "typ": TOKEN_TYPE},
        )
        return IssuedToken(token=token, jti=jti, issued_at=issued_at, expires_at=expires_at)


class TokenFailure(StrEnum):
    MISSING = "MISSING"
    MALFORMED = "MALFORMED"
    UNSUPPORTED_ALGORITHM = "UNSUPPORTED_ALGORITHM"
    WRONG_TYPE = "WRONG_TYPE"
    UNKNOWN_KEY = "UNKNOWN_KEY"
    BAD_SIGNATURE = "BAD_SIGNATURE"
    WRONG_ISSUER = "WRONG_ISSUER"
    WRONG_AUDIENCE = "WRONG_AUDIENCE"
    EXPIRED = "EXPIRED"
    NOT_YET_VALID = "NOT_YET_VALID"
    TTL_EXCEEDS_LIMIT = "TTL_EXCEEDS_LIMIT"
    ACTION_MISMATCH = "ACTION_MISMATCH"
    PROFILE_MISMATCH = "PROFILE_MISMATCH"
    UNKNOWN_TOKEN = "UNKNOWN_TOKEN"
    REVOKED = "REVOKED"


class TokenError(Exception):
    def __init__(self, failure: TokenFailure, message: str = "") -> None:
        super().__init__(message or failure.value)
        self.failure = failure


class TokenStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class RevocationChecker(Protocol):
    def status(self, jti: UUID) -> TokenStatus: ...


class SqlRevocationChecker:
    """Reads only (jti, revoked_at): works under remembrance_token_reader."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def status(self, jti: UUID) -> TokenStatus:
        with self._session_factory() as session:
            row = session.execute(
                select(AuthorizationToken.jti, AuthorizationToken.revoked_at).where(AuthorizationToken.jti == jti)
            ).first()
        if row is None:
            return TokenStatus.UNKNOWN
        return TokenStatus.REVOKED if row.revoked_at is not None else TokenStatus.ACTIVE


@dataclass(frozen=True)
class VerifiedAuthorization:
    jti: UUID
    actor_id: str
    action: ConsentAction
    profile_id: UUID
    grant_id: UUID | None
    purpose: str
    issued_at: datetime
    expires_at: datetime
    synthetic_disclosure_required: bool


_PYJWT_FAILURES: tuple[tuple[type[Exception], TokenFailure], ...] = (
    (jwt.InvalidSignatureError, TokenFailure.BAD_SIGNATURE),
    (jwt.InvalidAudienceError, TokenFailure.WRONG_AUDIENCE),
    (jwt.InvalidIssuerError, TokenFailure.WRONG_ISSUER),
    (jwt.PyJWTError, TokenFailure.MALFORMED),
)


class TokenValidator:
    def __init__(
        self,
        *,
        verification_keys: Mapping[str, Ed25519PublicKey],
        issuer: str,
        audience: str,
        revocation: RevocationChecker,
        clock: Clock,
        leeway_seconds: int = CLOCK_SKEW_SECONDS,
    ) -> None:
        self._keys = dict(verification_keys)
        self._issuer = issuer
        self._audience = audience
        self._revocation = revocation
        self._clock = clock
        self._leeway = leeway_seconds

    def validate(self, token: str | None, *, action: ConsentAction, profile_id: UUID) -> VerifiedAuthorization:
        if not token:
            raise TokenError(TokenFailure.MISSING)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenError(TokenFailure.MALFORMED, str(exc)) from exc
        if header.get("alg") != ALGORITHM:
            raise TokenError(TokenFailure.UNSUPPORTED_ALGORITHM, f"alg {header.get('alg')!r} is not accepted")
        if header.get("typ") != TOKEN_TYPE:
            raise TokenError(TokenFailure.WRONG_TYPE)
        key = self._keys.get(str(header.get("kid")))
        if key is None:
            raise TokenError(TokenFailure.UNKNOWN_KEY)
        try:
            # Time claims are checked below against the injected clock.
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": list(REQUIRED_CLAIMS), "verify_exp": False, "verify_nbf": False, "verify_iat": False},
            )
        except jwt.PyJWTError as exc:
            for error_type, failure in _PYJWT_FAILURES:
                if isinstance(exc, error_type):
                    raise TokenError(failure, str(exc)) from exc
            raise  # pragma: no cover - the PyJWTError row above catches everything
        try:
            iat, nbf, exp = int(claims["iat"]), int(claims["nbf"]), int(claims["exp"])
            jti = UUID(str(claims["jti"]))
            token_action = ConsentAction(claims["act"])
            token_profile = UUID(str(claims["pid"]))
            grant_id = UUID(str(claims["gid"])) if claims.get("gid") else None
        except (TypeError, ValueError, KeyError) as exc:
            raise TokenError(TokenFailure.MALFORMED, "claims have the wrong shape") from exc
        now = self._clock.now().timestamp()
        if exp - iat > MAX_TOKEN_TTL_SECONDS or exp <= iat:
            raise TokenError(TokenFailure.TTL_EXCEEDS_LIMIT)
        if now > exp + self._leeway:
            raise TokenError(TokenFailure.EXPIRED)
        if now + self._leeway < nbf:
            raise TokenError(TokenFailure.NOT_YET_VALID)
        if token_action != action:
            raise TokenError(TokenFailure.ACTION_MISMATCH)
        if token_profile != profile_id:
            raise TokenError(TokenFailure.PROFILE_MISMATCH)
        status = self._revocation.status(jti)
        if status is TokenStatus.UNKNOWN:
            raise TokenError(TokenFailure.UNKNOWN_TOKEN)
        if status is TokenStatus.REVOKED:
            raise TokenError(TokenFailure.REVOKED)
        return VerifiedAuthorization(
            jti=jti,
            actor_id=str(claims["sub"]),
            action=token_action,
            profile_id=token_profile,
            grant_id=grant_id,
            purpose=str(claims["pur"]),
            issued_at=datetime.fromtimestamp(iat, UTC),
            expires_at=datetime.fromtimestamp(exp, UTC),
            synthetic_disclosure_required=bool(claims.get("dsc", False)),
        )


def require_consent(
    action: ConsentAction, *, profile_param: str = "profile_id"
) -> Callable[[Request], VerifiedAuthorization]:
    """FastAPI dependency for downstream routes:

        @router.post("/voice/{profile_id}/synthesize")
        def synthesize(auth = Depends(require_consent(ConsentAction.INITIATE_VOICE_SYNTHESIS))): ...

    Expects `request.app.state.token_validator` and the kernel token in the
    X-Consent-Authorization header.
    """

    def dependency(request: Request) -> VerifiedAuthorization:
        validator: TokenValidator = request.app.state.token_validator
        try:
            profile_id = UUID(str(request.path_params[profile_param]))
        except (KeyError, ValueError) as exc:
            raise ApiError(400, "BAD_PROFILE_ID", f"path parameter {profile_param} must be a UUID") from exc
        try:
            return validator.validate(request.headers.get(CONSENT_TOKEN_HEADER), action=action, profile_id=profile_id)
        except TokenError as exc:
            raise ApiError(
                403, "CONSENT_REQUIRED", "a valid consent authorization token is required", failure=exc.failure.value
            ) from exc

    return dependency
