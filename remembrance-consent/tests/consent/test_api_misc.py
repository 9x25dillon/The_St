"""Successors, death records, pre-need lifecycle, operational endpoints."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.consent.tokens import KeyRing, private_pem
from app.main import create_app
from app.services import build_services
from tests.support import ADMIN, AUTH_SECRET, BEN_A, OWNER, SERVICE, STRANGER, SUCCESSOR, Person


def designate(client, profile_id, who=OWNER, successor=SUCCESSOR, priority=1):
    return client.post("/successors", json={"deceased_profile_id": profile_id, "successor_user_id": successor.user_id,
                                            "priority_order": priority}, headers=who.headers())


def record_death(client, profile_id, who=ADMIN, date="2026-09-01"):
    return client.post(f"/profiles/{profile_id}/death", json={"date_of_death": date, "death_certificate_sha256": "c" * 64},
                       headers=who.headers())


def test_successor_designation(api, client):
    grant = api.ready_grant()
    profile_id = grant["deceased_profile_id"]
    response = designate(client, profile_id)
    assert response.status_code == 201 and response.json()["designated_by"] == OWNER.user_id
    assert designate(client, profile_id, priority=2).json()["error"]["code"] == "SUCCESSOR_CONFLICT"
    assert designate(client, profile_id, successor=STRANGER, priority=1).status_code == 409
    assert designate(client, profile_id, who=ADMIN, successor=STRANGER, priority=2).status_code == 201
    assert designate(client, profile_id, who=BEN_A, successor=Person("x"), priority=3).json()["error"]["code"] == "OWNER_REQUIRED"
    assert designate(client, profile_id, who=Person("user-outsider"), successor=Person("y"), priority=4).status_code == 404
    assert designate(client, profile_id, who=SERVICE).status_code == 403
    assert designate(client, str(uuid4()), who=ADMIN).status_code == 404
    assert client.post("/successors", json={"deceased_profile_id": profile_id, "successor_user_id": "z", "priority_order": 0},
                       headers=OWNER.headers()).status_code == 422


def test_unverified_owner_cannot_designate(api, client):
    grant = api.create_grant()
    assert designate(client, grant["deceased_profile_id"]).json()["error"]["code"] == "OWNER_REQUIRED"


def test_record_death_rules(api, client):
    living = {"full_name": "Sam Subject", "date_of_birth": "1950-05-05", "jurisdiction_state": "US-TX"}
    grant = api.create_grant(Person("user-subject"), grantor_type="SELF_PRE_NEED", deceased_profile=living, beneficiaries=[])
    profile_id = grant["deceased_profile_id"]
    assert record_death(client, profile_id, who=OWNER).json()["error"]["code"] == "ADMIN_REQUIRED"
    assert record_death(client, profile_id, date="2031-01-01").json()["error"]["code"] == "DATE_OF_DEATH_IN_FUTURE"
    assert record_death(client, profile_id, date="1949-01-01").json()["error"]["code"] == "DATE_OF_DEATH_BEFORE_BIRTH"
    assert record_death(client, str(uuid4())).status_code == 404
    recorded = record_death(client, profile_id)
    assert recorded.status_code == 200 and recorded.json()["date_of_death"] == "2026-09-01"
    assert record_death(client, profile_id).json()["error"]["code"] == "DEATH_ALREADY_RECORDED"


def test_pre_need_lifecycle(api, client):
    """A living person consents for themselves and names a successor; on
    death their own account stops acting and the successor takes over."""
    subject = Person("user-subject", "subject@example.com")
    living = {"full_name": "Sam Subject", "date_of_birth": "1950-05-05", "jurisdiction_state": "US-TX"}
    grant = api.create_grant(subject, grantor_type="SELF_PRE_NEED", deceased_profile=living, beneficiaries=[],
                             purpose_scope=["MEMORIAL_VIEW", "VOICE_SYNTHESIS"])
    profile_id = grant["deceased_profile_id"]
    api.verify(grant["id"])
    assert designate(client, profile_id, who=subject).status_code == 201

    early = api.authorize(SUCCESSOR, "INITIATE_VOICE_SYNTHESIS", profile_id, "VOICE_SYNTHESIS")
    assert early.json()["reason"] == "SUBJECT_NOT_DECEASED"
    assert client.post(f"/export/{profile_id}", headers=subject.headers()).status_code == 201  # while alive

    assert record_death(client, profile_id).status_code == 200

    assert api.authorize(subject, "VIEW_MEMORIAL", profile_id, "MEMORIAL_VIEW").json()["reason"] == "DECEASED_CANNOT_ACT"
    assert client.post(f"/export/{profile_id}", headers=subject.headers()).status_code == 403
    assert designate(client, profile_id, who=subject, successor=STRANGER, priority=2).json()["error"]["code"] == "DECEASED_CANNOT_ACT"
    estate = api.create_grant(subject, deceased_profile=None, deceased_profile_id=profile_id, expect=403)
    assert estate["error"]["code"] == "DECEASED_CANNOT_ACT"
    late = api.authorize(SUCCESSOR, "INITIATE_VOICE_SYNTHESIS", profile_id, "VOICE_SYNTHESIS")
    assert late.status_code == 200 and late.json()["consent_grant_id"] == grant["id"]
    assert api.authorize(OWNER, "INITIATE_VOICE_SYNTHESIS", profile_id, "VOICE_SYNTHESIS").json()["reason"] == "NOT_AUTHORIZED"


def test_health_and_published_keys(client, services):
    assert client.get("/healthz").json() == {"status": "ok"}
    keys = client.get("/.well-known/consent-keys").json()
    assert keys["algorithm"] == "EdDSA" and set(keys["keys"]) == set(services.keyring.verification_keys)
    assert "BEGIN PUBLIC KEY" in next(iter(keys["keys"].values()))
    assert client.get("/openapi.json").status_code == 200


def test_production_hides_interactive_docs(engine, clock, storage, dispatcher):
    key = private_pem(KeyRing.generate().signing_key)
    settings = Settings(env="production", database_url="postgresql+psycopg://u@h/db", token_signing_key_pem=key,
                        auth_jwt_key=AUTH_SECRET, url_signing_secret="u" * 40)
    services = build_services(settings, engine=engine, clock=clock, storage=storage, dispatcher=dispatcher)
    with TestClient(create_app(services)) as client:
        assert client.get("/openapi.json").status_code == 401  # not public, and not routed
        assert client.get("/docs", headers=OWNER.headers()).status_code == 404
        assert client.get("/healthz").status_code == 200


def test_integrity_errors_become_409(app):
    @app.get("/boom")
    def boom():
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    with TestClient(app) as client:
        response = client.get("/boom", headers=OWNER.headers())
    assert response.status_code == 409 and response.json()["error"]["code"] == "CONFLICT"


@pytest.mark.parametrize("path", ["/consent/grants/not-a-uuid", "/consent/grants/not-a-uuid/audit"])
def test_path_validation(client, path):
    assert client.get(path, headers=OWNER.headers()).status_code == 422
