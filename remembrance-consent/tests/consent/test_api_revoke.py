"""POST /consent/grants/{id}/revoke: immediate effect, roles, idempotence."""
from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.consent.cascade import InlineDispatcher, run_cascade
from app.consent.constants import AuditEventType, ConsentAction, RevocationStatus
from app.consent.models import AuditEvent, RevocationRequest
from app.consent.tokens import TokenError, TokenFailure
from app.main import create_app
from app.services import build_services
from tests.factories import seed_artifacts
from tests.support import ADMIN, BEN_A, OWNER, SERVICE, STRANGER, SUCCESSOR, Api, Person


def designate(client, grant, who=SUCCESSOR):
    response = client.post("/successors", json={"deceased_profile_id": grant["deceased_profile_id"], "successor_user_id": who.user_id,
                                                "priority_order": 1}, headers=OWNER.headers())
    assert response.status_code == 201, response.text


def test_revocation_is_immediate(api, client, services, dispatcher):
    grant = api.ready_grant()
    profile_id = UUID(grant["deceased_profile_id"])
    token = api.authorize(OWNER, "INITIATE_VOICE_SYNTHESIS", grant["deceased_profile_id"], "VOICE_SYNTHESIS").json()["token"]

    result = api.revoke(grant["id"], reason="we changed our minds")

    assert result["status"] == "PENDING" and result["tokens_invalidated"] == 1
    with pytest.raises(TokenError) as caught:
        services.validator.validate(token, action=ConsentAction.INITIATE_VOICE_SYNTHESIS, profile_id=profile_id)
    assert caught.value.failure is TokenFailure.REVOKED
    after = api.authorize(OWNER, "INITIATE_VOICE_SYNTHESIS", grant["deceased_profile_id"], "VOICE_SYNTHESIS")
    assert after.status_code == 403 and after.json()["reason"] == "GRANT_REVOKED"
    body = client.get(f"/consent/grants/{grant['id']}", headers=OWNER.headers()).json()
    assert body["is_active"] is False and body["revocation_reason"] == "we changed our minds"
    assert dispatcher.dispatched == [UUID(grant["id"])]
    with services.session_factory() as s:
        [event] = s.scalars(select(AuditEvent).where(AuditEvent.event_type == AuditEventType.CONSENT_REVOKED)).all()
    assert event.payload["revoked_by_roles"] == ["OWNER"] and "changed" not in str(event.payload)


@pytest.mark.parametrize("who,roles", [(BEN_A, ["BENEFICIARY"]), (SUCCESSOR, ["SUCCESSOR"]), (ADMIN, ["ADMIN"])])
def test_any_authorized_party_can_revoke(api, client, services, who, roles):
    grant = api.ready_grant()
    designate(client, grant)
    api.revoke(grant["id"], who)
    with services.session_factory() as s:
        [event] = s.scalars(select(AuditEvent).where(AuditEvent.event_type == AuditEventType.CONSENT_REVOKED)).all()
    assert event.payload["revoked_by_roles"] == roles


def test_outsiders_cannot_revoke(api):
    grant = api.ready_grant()
    api.revoke(grant["id"], STRANGER, expect=404)
    api.revoke(grant["id"], SERVICE, expect=403)
    api.revoke("00000000-0000-0000-0000-000000000000", OWNER, expect=404)


def test_repeat_revocation_is_idempotent(api, services, dispatcher, clock, storage):
    grant = api.ready_grant()
    first = api.revoke(grant["id"])
    second = api.revoke(grant["id"], BEN_A, reason="again")
    assert second["revocation_request_id"] == first["revocation_request_id"]
    assert second["tokens_invalidated"] == 0 and second["revoked_at"] == first["revoked_at"]
    assert len(dispatcher.dispatched) == 2  # an unfinished cascade is re-dispatched
    run_cascade(UUID(grant["id"]), session_factory=services.session_factory, storage=storage, clock=clock)
    third = api.revoke(grant["id"])
    assert third["status"] == "COMPLETE" and len(dispatcher.dispatched) == 2
    with services.session_factory() as s:
        assert len(s.scalars(select(RevocationRequest)).all()) == 1
        assert len(s.scalars(select(AuditEvent).where(AuditEvent.event_type == AuditEventType.CONSENT_REVOKED)).all()) == 1


def test_enqueue_failure_does_not_undo_revocation(api, dispatcher, caplog):
    def broken(_):
        raise ConnectionError("broker down")

    dispatcher.dispatch = broken
    grant = api.ready_grant()
    assert api.revoke(grant["id"])["status"] == "PENDING"
    assert "sweeper will retry" in caplog.text
    assert api.authorize(OWNER, "VIEW_MEMORIAL", grant["deceased_profile_id"], "MEMORIAL_VIEW").status_code == 403


def test_inline_cascade_destroys_derived_artifacts_end_to_end(settings, engine, clock, keyring, storage):
    holder = {}
    dispatcher = InlineDispatcher(lambda gid: run_cascade(gid, session_factory=holder["s"].session_factory, storage=storage, clock=clock))
    services = build_services(settings, engine=engine, clock=clock, keyring=keyring, storage=storage, dispatcher=dispatcher)
    holder["s"] = services
    with TestClient(create_app(services)) as client:
        api = Api(client)
        grant = api.ready_grant()
        seed_artifacts(services.session_factory, clock, storage, UUID(grant["deceased_profile_id"]), UUID(grant["id"]))
        assert api.revoke(grant["id"])["status"] == "PENDING"
        again = api.revoke(grant["id"])
    assert again["status"] == RevocationStatus.COMPLETE.value
    assert not any(p.is_file() for p in (storage.root / "models").rglob("*"))
    assert not any(p.is_file() for p in (storage.root / "audio").rglob("*"))


def test_deceased_subject_cannot_revoke(api, client):
    subject = Person("user-subject", "subject@example.com")
    living = {"full_name": "Sam Subject", "jurisdiction_state": "US-TX"}
    grant = api.create_grant(subject, grantor_type="SELF_PRE_NEED", deceased_profile=living, beneficiaries=[])
    api.verify(grant["id"])
    response = client.post(f"/profiles/{grant['deceased_profile_id']}/death",
                           json={"date_of_death": "2026-09-01", "death_certificate_sha256": "c" * 64}, headers=ADMIN.headers())
    assert response.status_code == 200
    denied = api.revoke(grant["id"], subject, expect=403)
    assert denied["error"]["code"] == "DECEASED_CANNOT_ACT"
