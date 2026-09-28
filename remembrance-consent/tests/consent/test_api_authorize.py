"""POST /consent/authorize: purpose denylist, kernel decisions, rate limit."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.consent.middleware as middleware
from app.consent.constants import BANNED_PURPOSES, UNATTRIBUTED_PROFILE_ID, AuditEventType, ConsentAction
from app.consent.models import AuditEvent, AuthorizationToken
from app.main import create_app
from app.ratelimit import InMemoryGCRA
from app.services import build_services
from tests.support import BEN_A, OWNER, SERVICE, STRANGER, Api

BANNED_VARIANTS = sorted(BANNED_PURPOSES) + ["financial", "Property Claim", "legal-representation", "COMMERCIAL_IMPERSONATION_V2"]


def violations(services):
    with services.session_factory() as s:
        return s.scalars(select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PURPOSE_VIOLATION_ATTEMPT)).all()


@pytest.mark.parametrize("purpose", BANNED_VARIANTS)
@pytest.mark.parametrize("action", [ConsentAction.VIEW_MEMORIAL, ConsentAction.INITIATE_VOICE_SYNTHESIS])
def test_every_banned_purpose_is_refused_at_the_boundary_and_audited(api, services, purpose, action):
    grant = api.ready_grant()
    response = api.authorize(OWNER, action.value, grant["deceased_profile_id"], purpose)

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "PURPOSE_VIOLATION"
    [event] = violations(services)
    assert event.id == error["audit_event_id"]
    assert str(event.deceased_profile_id) == grant["deceased_profile_id"] and event.actor_id == OWNER.user_id
    assert event.payload["detected_by"] == "middleware" and event.payload["route"] == "/consent/authorize"
    with services.session_factory() as s:
        assert s.scalars(select(AuthorizationToken)).all() == []


def test_banned_purpose_with_malformed_profile_is_still_audited(client, services):
    response = client.post(
        "/consent/authorize",
        json={"action": "VIEW_MEMORIAL", "profile_id": "not-a-uuid", "purpose_code": "FINANCIAL"},
        headers=STRANGER.headers(),
    )
    assert response.status_code == 403
    [event] = violations(services)
    assert event.deceased_profile_id == UNATTRIBUTED_PROFILE_ID and "requested_profile_id" not in event.payload


def test_kernel_catches_banned_purposes_the_middleware_did_not_see(services, monkeypatch):
    monkeypatch.setattr(middleware, "GUARDED_ROUTES", {})
    with TestClient(create_app(services)) as client:
        api = Api(client)
        grant = api.ready_grant()
        response = api.authorize(OWNER, "VIEW_MEMORIAL", grant["deceased_profile_id"], "FINANCIAL")
    assert response.status_code == 403 and response.json()["error"]["code"] == "PURPOSE_VIOLATION"
    [event] = violations(services)
    assert event.payload["detected_by"] == "kernel"


def test_allowed_request_returns_a_short_lived_token(api, services):
    grant = api.ready_grant()
    response = api.authorize(OWNER, "GENERATE_SCRIPT", grant["deceased_profile_id"], "script-generation")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["allowed"] and body["token_type"] == "consent+jwt" and body["consent_grant_id"] == grant["id"]
    verified = services.validator.validate(body["token"], action=ConsentAction.GENERATE_SCRIPT, profile_id=uuid_of(grant["deceased_profile_id"]))
    assert str(verified.jti) == body["jti"]


def uuid_of(text):
    from uuid import UUID

    return UUID(text)


def test_denials_disclose_reasons_only_to_related_people(api):
    grant = api.create_grant()  # unverified
    profile_id = grant["deceased_profile_id"]
    owner = api.authorize(OWNER, "INITIATE_VOICE_SYNTHESIS", profile_id, "VOICE_SYNTHESIS")
    assert owner.status_code == 403 and owner.json()["reason"] == "AUTHORITY_NOT_VERIFIED"
    beneficiary = api.authorize(BEN_A, "VIEW_MEMORIAL", profile_id, "MEMORIAL_VIEW")
    assert beneficiary.json()["reason"] == "AUTHORITY_NOT_VERIFIED"
    stranger = api.authorize(STRANGER, "VIEW_MEMORIAL", profile_id, "MEMORIAL_VIEW")
    assert stranger.json() | {"profile_id": None} == {
        "allowed": False, "action": "VIEW_MEMORIAL", "profile_id": None, "consent_grant_id": None, "token": None,
        "token_type": None, "jti": None, "expires_at": None, "reason": "NOT_AUTHORIZED",
    }
    assert api.authorize(SERVICE, "VIEW_MEMORIAL", profile_id, "MEMORIAL_VIEW").json()["reason"] == "NON_HUMAN_ACTOR"
    assert api.authorize(OWNER, "VIEW_MEMORIAL", str(uuid4()), "MEMORIAL_VIEW").json()["reason"] == "NOT_AUTHORIZED"


def test_request_shape_is_validated(client):
    headers = {**OWNER.headers(), "Content-Type": "application/json"}
    assert client.post("/consent/authorize", content=b"{not json", headers=headers).status_code == 422
    assert client.post("/consent/authorize", json={"action": "SELL_VOICE", "profile_id": str(uuid4()), "purpose_code": "X"},
                       headers=OWNER.headers()).status_code == 422
    big = {"action": "VIEW_MEMORIAL", "profile_id": str(uuid4()), "purpose_code": "x" * 70_000}
    response = client.post("/consent/authorize", json=big, headers=OWNER.headers())
    assert response.status_code == 413 and response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_rate_limit_is_per_actor(settings, engine, clock, keyring, storage, dispatcher):
    services = build_services(settings, engine=engine, clock=clock, keyring=keyring, storage=storage, dispatcher=dispatcher,
                              rate_limiter=InMemoryGCRA(rate_per_minute=1, burst=2))
    with TestClient(create_app(services)) as client:
        api = Api(client)
        profile_id = str(uuid4())
        codes = [api.authorize(OWNER, "VIEW_MEMORIAL", profile_id, "MEMORIAL_VIEW").status_code for _ in range(3)]
        assert codes == [403, 403, 429]
        limited = api.authorize(OWNER, "VIEW_MEMORIAL", profile_id, "FINANCIAL")
        assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert api.authorize(STRANGER, "VIEW_MEMORIAL", profile_id, "MEMORIAL_VIEW").status_code == 403
        # Other routes are not throttled.
        assert client.get("/healthz").status_code == 200
    assert violations(services) == []  # the throttled banned request never reached the audit log
