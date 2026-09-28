"""Grant lifecycle over HTTP: create, verify, acknowledge, read, audit."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.consent import acknowledgments
from app.consent.constants import BANNED_PURPOSES, UNATTRIBUTED_PROFILE_ID, AuditEventType
from app.consent.models import AuditEvent, ConsentGrant
from tests.support import ADMIN, BEN_A, BEN_B, DOCUMENT_SHA, OWNER, SERVICE, STRANGER, SUCCESSOR, Person, grant_payload


def audit(services, **filters):
    with services.session_factory() as s:
        statement = select(AuditEvent).order_by(AuditEvent.id)
        for column, value in filters.items():
            statement = statement.where(getattr(AuditEvent, column) == value)
        return s.scalars(statement).all()


# --- creation -----------------------------------------------------------------

def test_create_grant_with_inline_profile(api, services):
    grant = api.create_grant(beneficiaries=[{"name": "Ben A", "email": "Ben.A@Example.com"}])
    assert grant["grantor_user_id"] == OWNER.user_id and grant["is_active"] is True
    assert grant["authority_verified_at"] is None and grant["revoked_at"] is None
    assert grant["purpose_scope"] == ["MEMORIAL_VIEW", "SCRIPT_GENERATION", "VOICE_SYNTHESIS"]
    assert grant["beneficiaries"] == [{"name": "Ben A", "email": "ben.a@example.com", "acknowledged_at": None}]
    assert grant["quorum"] == {"required": 1, "acknowledged": 0, "satisfied": False}
    events = audit(services, deceased_profile_id=uuid_of(grant["deceased_profile_id"]))
    assert [e.event_type for e in events] == [AuditEventType.PROFILE_CREATED, AuditEventType.CONSENT_CREATED]
    created = events[1].payload
    assert created["beneficiary_count"] == 1 and len(created["legal_authority_document_url_sha256"]) == 64
    assert "owner@example.com" not in str(created)  # no raw personal data in the ledger


def uuid_of(text):
    from uuid import UUID

    return UUID(text)


def test_create_second_grant_on_existing_profile(api):
    first = api.create_grant()
    second = api.create_grant(BEN_A, deceased_profile=None, deceased_profile_id=first["deceased_profile_id"], grantor_type="BENEFICIARY")
    assert second["deceased_profile_id"] == first["deceased_profile_id"]
    api.create_grant(deceased_profile=None, deceased_profile_id=str(uuid4()), expect=404)


@pytest.mark.parametrize(
    "overrides",
    [
        {"legal_authority_document_url": "http://documents.example/l.pdf"},
        {"legal_authority_document_url": "https://user:pw@documents.example/l.pdf"},
        {"legal_authority_document_url": "https://documents.example/" + "x" * 2100},
        {"deceased_profile_id": "00000000-0000-0000-0000-000000000001"},  # both profile forms
        {"deceased_profile": None},  # neither
        {"purpose_scope": []},
        {"purpose_scope": ["VOICE_SYNTHESIS", "VOICE_SYNTHESIS"]},
        {"purpose_scope": ["HISTORICAL_RESEARCH"]},
        {"beneficiaries": [{"name": "A", "email": "dup@x.test"}, {"name": "B", "email": "DUP@x.test"}]},
        {"required_acknowledgments": 3},
        {"is_active": False},  # smuggled lifecycle field
        {"authority_verified_at": "2026-01-01T00:00:00Z"},
        {"grantor_type": "EXECUTOR_OF_WILL"},
        {"deceased_profile": {"full_name": "E", "date_of_birth": "2026-02-01", "date_of_death": "2026-01-01", "jurisdiction_state": "US-CA"}},
        {"deceased_profile": {"full_name": "E", "date_of_death": "2026-01-01", "jurisdiction_state": "California"}},
    ],
)
def test_invalid_grants_are_rejected_without_echoing_input(client, overrides):
    response = client.post("/consent/grants", json=grant_payload(**overrides), headers=OWNER.headers())
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_FAILED"
    assert all(set(problem) == {"loc", "msg", "type"} for problem in body["problems"])  # submitted values never echoed


def test_estate_consent_requires_a_recorded_death(client):
    profile = {"full_name": "E", "jurisdiction_state": "US-CA"}
    response = client.post("/consent/grants", json=grant_payload(deceased_profile=profile), headers=OWNER.headers())
    assert response.status_code == 422 and response.json()["error"]["code"] == "ESTATE_BEFORE_DEATH"


def test_pre_need_consent_cannot_follow_death(client):
    response = client.post("/consent/grants", json=grant_payload(grantor_type="SELF_PRE_NEED"), headers=OWNER.headers())
    assert response.status_code == 422 and response.json()["error"]["code"] == "PRE_NEED_AFTER_DEATH"


def test_future_death_is_rejected(client):
    profile = {"full_name": "E", "date_of_death": "2030-01-01", "jurisdiction_state": "US-CA"}
    response = client.post("/consent/grants", json=grant_payload(deceased_profile=profile), headers=OWNER.headers())
    assert response.json()["error"]["code"] == "DATE_OF_DEATH_IN_FUTURE"


def test_only_one_account_can_be_the_pre_need_subject(api, client):
    living = {"full_name": "Sam Subject", "jurisdiction_state": "US-TX"}
    grant = api.create_grant(STRANGER, grantor_type="SELF_PRE_NEED", deceased_profile=living, beneficiaries=[])
    impostor = grant_payload(grantor_type="SELF_PRE_NEED", deceased_profile=None, deceased_profile_id=grant["deceased_profile_id"], beneficiaries=[])
    response = client.post("/consent/grants", json=impostor, headers=OWNER.headers())
    assert response.status_code == 409 and response.json()["error"]["code"] == "PRE_NEED_SUBJECT_CONFLICT"
    api.create_grant(STRANGER, grantor_type="SELF_PRE_NEED", deceased_profile=None, deceased_profile_id=grant["deceased_profile_id"], beneficiaries=[])


def test_services_cannot_grant_consent(client):
    response = client.post("/consent/grants", json=grant_payload(), headers=SERVICE.headers())
    assert response.status_code == 403 and response.json()["error"]["code"] == "NON_HUMAN_ACTOR"


@pytest.mark.parametrize("banned", sorted(BANNED_PURPOSES))
def test_banned_purpose_in_scope_is_refused_and_audited(client, services, banned):
    response = client.post("/consent/grants", json=grant_payload(purpose_scope=["MEMORIAL_VIEW", banned.lower()]), headers=OWNER.headers())
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "PURPOSE_VIOLATION"
    [event] = audit(services)
    assert event.id == error["audit_event_id"] and event.event_type is AuditEventType.PURPOSE_VIOLATION_ATTEMPT
    assert event.payload["purpose_codes"] == [banned] and event.payload["route"] == "/consent/grants"
    assert event.deceased_profile_id == UNATTRIBUTED_PROFILE_ID
    with services.session_factory() as s:
        assert s.scalars(select(ConsentGrant)).all() == []


def test_authentication_is_required(client):
    assert client.post("/consent/grants", json=grant_payload()).status_code == 401
    bad = {"Authorization": "Bearer not.a.jwt"}
    response = client.post("/consent/grants", json=grant_payload(), headers=bad)
    assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"


# --- verification -------------------------------------------------------------

def test_admin_verifies_authority_once(api, client, services):
    grant = api.create_grant()
    verified = api.verify(grant["id"])
    assert verified["authority_verified_by"] == ADMIN.user_id and verified["authority_verified_at"]
    [event] = audit(services, event_type=AuditEventType.AUTHORITY_VERIFIED)
    assert event.payload == {"document_sha256": DOCUMENT_SHA}
    again = client.post(f"/consent/grants/{grant['id']}/verify", json={"document_sha256": DOCUMENT_SHA}, headers=ADMIN.headers())
    assert again.status_code == 409 and again.json()["error"]["code"] == "ALREADY_VERIFIED"


def test_verification_rules(api, client):
    grant = api.create_grant()
    url = f"/consent/grants/{grant['id']}/verify"
    body = {"document_sha256": DOCUMENT_SHA}
    assert client.post(url, json=body, headers=OWNER.headers()).json()["error"]["code"] == "ADMIN_REQUIRED"
    assert client.post(url, json={"document_sha256": "XYZ"}, headers=ADMIN.headers()).status_code == 422
    assert client.post(f"/consent/grants/{uuid4()}/verify", json=body, headers=ADMIN.headers()).status_code == 404
    own = api.create_grant(ADMIN)
    response = client.post(f"/consent/grants/{own['id']}/verify", json=body, headers=ADMIN.headers())
    assert response.status_code == 403 and response.json()["error"]["code"] == "SELF_VERIFICATION"
    api.revoke(grant["id"])
    assert client.post(url, json=body, headers=ADMIN.headers()).json()["error"]["code"] == "GRANT_REVOKED"


# --- acknowledgment -----------------------------------------------------------

def test_beneficiaries_acknowledge_and_quorum_fills(api, services):
    grant = api.create_grant()
    first = api.acknowledge(grant["id"], BEN_A)
    assert first["quorum"] == {"required": 2, "acknowledged": 1, "satisfied": False}
    statement = first["statement"]
    assert statement["purpose_scope"] == grant["purpose_scope"] and statement["beneficiary_email"] == BEN_A.email
    assert first["acknowledgment_signature_hash"] == acknowledgments.signature_hash(statement, f"/s/ {BEN_A.user_id}")
    second = api.acknowledge(grant["id"], BEN_B)
    assert second["quorum"]["satisfied"] is True
    events = audit(services, event_type=AuditEventType.BENEFICIARY_ACKNOWLEDGED)
    assert [e.payload["quorum_acknowledged"] for e in events] == [1, 2]
    assert events[0].payload["statement_sha256"] == acknowledgments.statement_digest(statement)


def test_acknowledgment_rules(api, client):
    grant = api.create_grant()
    api.acknowledge(grant["id"], BEN_A)
    assert api.acknowledge(grant["id"], BEN_A, expect=409)["error"]["code"] == "ALREADY_ACKNOWLEDGED"
    api.acknowledge(grant["id"], STRANGER, expect=404)
    unverified_email = Person(BEN_B.user_id, BEN_B.email, email_verified=False)
    api.acknowledge(grant["id"], unverified_email, expect=404)
    api.acknowledge(grant["id"], SERVICE, expect=403)
    api.revoke(grant["id"], BEN_A)
    assert api.acknowledge(grant["id"], BEN_B, expect=409)["error"]["code"] == "GRANT_REVOKED"


# --- reads --------------------------------------------------------------------

def test_grant_is_readable_by_owner_and_admin_only(api, client):
    grant = api.ready_grant()
    url = f"/consent/grants/{grant['id']}"
    body = client.get(url, headers=OWNER.headers()).json()
    assert body["quorum"]["satisfied"] and all(b["acknowledged_at"] for b in body["beneficiaries"])
    assert client.get(url, headers=ADMIN.headers()).status_code == 200
    for outsider in (BEN_A, STRANGER):
        assert client.get(url, headers=outsider.headers()).status_code == 404
    assert client.get(f"/consent/grants/{uuid4()}", headers=ADMIN.headers()).status_code == 404


def test_audit_endpoint_pages_through_a_verified_chain(api, client):
    grant = api.ready_grant()
    client.post("/successors", json={"deceased_profile_id": grant["deceased_profile_id"], "successor_user_id": SUCCESSOR.user_id,
                                     "priority_order": 1}, headers=OWNER.headers())
    url = f"/consent/grants/{grant['id']}/audit"
    page = client.get(url, params={"limit": 2}, headers=BEN_A.headers()).json()
    assert [e["event_type"] for e in page["events"]] == ["CONSENT_CREATED", "AUTHORITY_VERIFIED"]
    assert page["chain"]["verified"] is True and page["chain"]["length"] == 6 and page["chain"]["errors"] == 0
    rest = client.get(url, params={"after_id": page["next_after_id"]}, headers=SUCCESSOR.headers()).json()
    assert [e["event_type"] for e in rest["events"]] == ["BENEFICIARY_ACKNOWLEDGED", "BENEFICIARY_ACKNOWLEDGED"]
    assert rest["next_after_id"] is None
    assert client.get(url, headers=ADMIN.headers()).status_code == 200
    assert client.get(url, headers=STRANGER.headers()).status_code == 404
    assert client.get(url, params={"limit": 501}, headers=OWNER.headers()).status_code == 422
