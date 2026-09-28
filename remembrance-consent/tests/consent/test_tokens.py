from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import Settings
from app.consent.constants import ConsentAction
from app.consent.tokens import (
    KeyRing,
    SqlRevocationChecker,
    TokenError,
    TokenFailure,
    TokenIssuer,
    TokenStatus,
    TokenValidator,
    private_pem,
    public_pem,
)
from tests.factories import seed_grant, seed_profile, seed_token

A = ConsentAction
PROFILE = uuid4()


class Ledger:
    def __init__(self) -> None:
        self.status_by_jti: dict[UUID, TokenStatus] = {}

    def status(self, jti: UUID) -> TokenStatus:
        return self.status_by_jti.get(jti, TokenStatus.UNKNOWN)


@pytest.fixture
def ledger() -> Ledger:
    return Ledger()


@pytest.fixture
def issuer(keyring) -> TokenIssuer:
    return TokenIssuer(keyring, issuer="iss", audience="aud")


@pytest.fixture
def validator(keyring, ledger, clock) -> TokenValidator:
    return TokenValidator(verification_keys=keyring.verification_keys, issuer="iss", audience="aud", revocation=ledger, clock=clock)


def issue(issuer, ledger, clock, action=A.INITIATE_VOICE_SYNTHESIS, profile=PROFILE, active=True):
    token = issuer.issue(jti=uuid4(), actor_id="u", action=action, profile_id=profile, grant_id=uuid4(), purpose="VOICE_SYNTHESIS", now=clock.now())
    if active:
        ledger.status_by_jti[token.jti] = TokenStatus.ACTIVE
    return token


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def hand_rolled(header: dict, claims: dict, sign) -> str:
    """Build a JWT without PyJWT's guard rails, as an attacker would."""
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    return f"{signing_input}.{_b64(sign(signing_input.encode()))}"


def forge(keyring, clock, *, headers=None, key=None, algorithm="EdDSA", **claims):
    now = int(clock.now().timestamp())
    payload = {"iss": "iss", "aud": "aud", "sub": "u", "jti": str(uuid4()), "iat": now, "nbf": now, "exp": now + 60,
               "act": A.VIEW_MEMORIAL.value, "pid": str(PROFILE), "gid": None, "pur": "MEMORIAL_VIEW"}
    payload.update(claims)
    header = {"kid": keyring.signing_kid, "typ": "consent+jwt"}
    header.update(headers or {})
    return jwt.encode(payload, key or keyring.signing_key, algorithm=algorithm, headers=header)


def failure(validator, token, action=A.VIEW_MEMORIAL, profile=PROFILE) -> TokenFailure:
    with pytest.raises(TokenError) as caught:
        validator.validate(token, action=action, profile_id=profile)
    return caught.value.failure


def test_round_trip_carries_scope_and_disclosure_flag(issuer, validator, ledger, clock):
    token = issue(issuer, ledger, clock)
    verified = validator.validate(token.token, action=A.INITIATE_VOICE_SYNTHESIS, profile_id=PROFILE)
    assert verified.jti == token.jti and verified.synthetic_disclosure_required
    assert verified.expires_at - verified.issued_at == timedelta(seconds=300)
    view = issue(issuer, ledger, clock, action=A.VIEW_MEMORIAL)
    assert not validator.validate(view.token, action=A.VIEW_MEMORIAL, profile_id=PROFILE).synthetic_disclosure_required


def test_header_names_key_and_type(issuer, ledger, clock, keyring):
    header = jwt.get_unverified_header(issue(issuer, ledger, clock).token)
    assert header == {"alg": "EdDSA", "kid": keyring.signing_kid, "typ": "consent+jwt"}


@pytest.mark.parametrize("ttl", [0, 301, 3600])
def test_issuer_refuses_lifetimes_over_five_minutes(keyring, ttl):
    with pytest.raises(ValueError):
        TokenIssuer(keyring, issuer="iss", audience="aud", ttl_seconds=ttl)


def test_missing_and_malformed(validator):
    assert failure(validator, None) is TokenFailure.MISSING
    assert failure(validator, "") is TokenFailure.MISSING
    assert failure(validator, "not-a-jwt") is TokenFailure.MALFORMED


def test_alg_none_and_hmac_confusion_are_rejected(validator, keyring, clock, ledger):
    claims = jwt.decode(forge(keyring, clock), options={"verify_signature": False})
    unsigned = hand_rolled({"alg": "none", "kid": keyring.signing_kid, "typ": "consent+jwt"}, claims, lambda _: b"")
    assert failure(validator, unsigned) is TokenFailure.UNSUPPORTED_ALGORITHM
    # HS256 keyed with the public key bytes: the classic key-confusion attack.
    pub = public_pem(keyring.signing_key.public_key()).encode()
    confused = hand_rolled(
        {"alg": "HS256", "kid": keyring.signing_kid, "typ": "consent+jwt"},
        claims,
        lambda message: hmac.new(pub, message, hashlib.sha256).digest(),
    )
    assert failure(validator, confused) is TokenFailure.UNSUPPORTED_ALGORITHM


def test_type_and_key_id_are_enforced(validator, keyring, clock):
    assert failure(validator, forge(keyring, clock, headers={"typ": "JWT"})) is TokenFailure.WRONG_TYPE
    assert failure(validator, forge(keyring, clock, headers={"kid": "someone-else"})) is TokenFailure.UNKNOWN_KEY


def test_signature_from_another_key_with_our_kid(validator, keyring, clock):
    impostor = forge(keyring, clock, key=Ed25519PrivateKey.generate())
    assert failure(validator, impostor) is TokenFailure.BAD_SIGNATURE


def test_tampered_payload_fails_signature(issuer, validator, ledger, clock):
    token = issue(issuer, ledger, clock, action=A.VIEW_MEMORIAL).token
    head, body, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    claims["act"] = A.DELIVER_MESSAGE.value
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    assert failure(validator, f"{head}.{body}.{sig}", action=A.DELIVER_MESSAGE) is TokenFailure.BAD_SIGNATURE


def test_issuer_audience_and_claim_shape(validator, keyring, clock):
    assert failure(validator, forge(keyring, clock, iss="elsewhere")) is TokenFailure.WRONG_ISSUER
    assert failure(validator, forge(keyring, clock, aud="elsewhere")) is TokenFailure.WRONG_AUDIENCE
    assert failure(validator, forge(keyring, clock, act="LAUNDER_MONEY")) is TokenFailure.MALFORMED
    assert failure(validator, forge(keyring, clock, pid="not-a-uuid")) is TokenFailure.MALFORMED
    no_purpose = forge(keyring, clock)
    claims = jwt.decode(no_purpose, options={"verify_signature": False})
    del claims["pur"]
    stripped = jwt.encode(claims, keyring.signing_key, algorithm="EdDSA", headers={"kid": keyring.signing_kid, "typ": "consent+jwt"})
    assert failure(validator, stripped) is TokenFailure.MALFORMED


def test_lifetime_cap_is_enforced_even_on_validly_signed_tokens(validator, keyring, clock):
    now = int(clock.now().timestamp())
    assert failure(validator, forge(keyring, clock, exp=now + 3600)) is TokenFailure.TTL_EXCEEDS_LIMIT
    assert failure(validator, forge(keyring, clock, exp=now)) is TokenFailure.TTL_EXCEEDS_LIMIT


def test_time_window(issuer, validator, ledger, clock):
    token = issue(issuer, ledger, clock, action=A.VIEW_MEMORIAL).token
    clock.advance(seconds=300 + 4)  # inside leeway
    validator.validate(token, action=A.VIEW_MEMORIAL, profile_id=PROFILE)
    clock.advance(seconds=2)
    assert failure(validator, token) is TokenFailure.EXPIRED
    early = issue(issuer, ledger, clock, action=A.VIEW_MEMORIAL).token
    clock.advance(seconds=-60)
    assert failure(validator, early) is TokenFailure.NOT_YET_VALID


def test_scope_mismatches(issuer, validator, ledger, clock):
    token = issue(issuer, ledger, clock).token
    assert failure(validator, token, action=A.DELIVER_MESSAGE) is TokenFailure.ACTION_MISMATCH
    assert failure(validator, token, action=A.INITIATE_VOICE_SYNTHESIS, profile=uuid4()) is TokenFailure.PROFILE_MISMATCH


def test_ledger_fails_closed(issuer, validator, ledger, clock):
    unknown = issue(issuer, ledger, clock, active=False)
    assert failure(validator, unknown.token, action=A.INITIATE_VOICE_SYNTHESIS) is TokenFailure.UNKNOWN_TOKEN
    revoked = issue(issuer, ledger, clock)
    ledger.status_by_jti[revoked.jti] = TokenStatus.REVOKED
    assert failure(validator, revoked.token, action=A.INITIATE_VOICE_SYNTHESIS) is TokenFailure.REVOKED


def test_sql_revocation_checker(session_factory, clock):
    checker = SqlRevocationChecker(session_factory)
    profile_id = seed_profile(session_factory, clock)
    jti = seed_token(session_factory, clock, profile_id, seed_grant(session_factory, clock, profile_id))
    assert checker.status(jti) is TokenStatus.ACTIVE
    assert checker.status(uuid4()) is TokenStatus.UNKNOWN
    from app.consent.models import AuthorizationToken

    with session_factory.begin() as s:
        s.get(AuthorizationToken, jti).revoked_at = clock.now()
    assert checker.status(jti) is TokenStatus.REVOKED


def test_keyring_from_settings(tmp_path):
    key = Ed25519PrivateKey.generate()
    old = Ed25519PrivateKey.generate().public_key()
    settings = Settings(
        env="test",
        token_signing_key_pem=json.dumps(private_pem(key))[1:-1],  # escaped newlines, as in .env files
        token_key_id="k2",
        token_verification_keys_json=json.dumps({"k1": public_pem(old)}),
    )
    ring = KeyRing.from_settings(settings)
    assert ring.signing_kid == "k2" and set(ring.verification_keys) == {"k1", "k2"}
    assert set(ring.public_keys_pem()) == {"k1", "k2"}
    ephemeral = KeyRing.from_settings(Settings(env="development", token_key_id="dev"))
    assert list(ephemeral.verification_keys) == ["dev"]


def test_keyring_rejects_non_ed25519_keys():
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_private = rsa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    rsa_public = rsa_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    with pytest.raises(ValueError):
        KeyRing.from_settings(Settings(env="test", token_signing_key_pem=rsa_private))
    with pytest.raises(ValueError):
        KeyRing.from_settings(Settings(env="test", token_verification_keys_json=json.dumps({"x": rsa_public})))
