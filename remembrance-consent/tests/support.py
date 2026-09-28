"""Shared test vocabulary: identities, request builders and an API driver."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import jwt
from fastapi.testclient import TestClient

AUTH_SECRET = "test-identity-assertion-secret-0123456789abcdef"
AUTH_ISSUER = "remembrance-web"
AUTH_AUDIENCE = "remembrance-consent"
DOCUMENT_SHA = "a" * 64


@dataclass(frozen=True)
class Person:
    user_id: str
    email: str | None = None
    admin: bool = False
    kind: str = "human"
    email_verified: bool = True

    def headers(self, **claims: Any) -> dict[str, str]:
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": self.user_id,
            "iss": AUTH_ISSUER,
            "aud": AUTH_AUDIENCE,
            "iat": now,
            "exp": now + 300,
            "roles": ["admin"] if self.admin else [],
            "actor_kind": self.kind,
        }
        if self.email:
            payload["email"] = self.email
            payload["email_verified"] = self.email_verified
        payload.update(claims)
        return {"Authorization": "Bearer " + jwt.encode(payload, AUTH_SECRET, algorithm="HS256")}


OWNER = Person("user-owner", "owner@example.com")
ADMIN = Person("user-admin", "admin@example.com", admin=True)
SECOND_ADMIN = Person("user-admin-2", "admin2@example.com", admin=True)
BEN_A = Person("user-ben-a", "ben.a@example.com")
BEN_B = Person("user-ben-b", "ben.b@example.com")
STRANGER = Person("user-stranger", "stranger@example.com")
SUCCESSOR = Person("user-successor", "successor@example.com")
SERVICE = Person("svc-synthesis", kind="service")


def grant_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "deceased_profile": {
            "full_name": "Eleanor Example",
            "date_of_birth": "1942-03-04",
            "date_of_death": "2026-01-15",
            "jurisdiction_state": "US-CA",
        },
        "grantor_type": "EXECUTOR",
        "grantor_legal_name": "Olivia Owner",
        "grantor_contact_email": "owner@example.com",
        "legal_authority_document_url": "https://documents.example/letters-testamentary.pdf",
        "purpose_scope": ["MEMORIAL_VIEW", "VOICE_SYNTHESIS", "SCRIPT_GENERATION"],
        "beneficiaries": [
            {"name": "Ben A", "email": BEN_A.email},
            {"name": "Ben B", "email": BEN_B.email},
        ],
    }
    payload.update(overrides)
    return payload


class Api:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def create_grant(self, who: Person = OWNER, expect: int = 201, **overrides: Any) -> dict[str, Any]:
        response = self.client.post("/consent/grants", json=grant_payload(**overrides), headers=who.headers())
        assert response.status_code == expect, response.text
        return response.json()

    def verify(self, grant_id: str, who: Person = ADMIN, expect: int = 200) -> dict[str, Any]:
        response = self.client.post(f"/consent/grants/{grant_id}/verify", json={"document_sha256": DOCUMENT_SHA}, headers=who.headers())
        assert response.status_code == expect, response.text
        return response.json()

    def acknowledge(self, grant_id: str, who: Person, expect: int = 201) -> dict[str, Any]:
        response = self.client.post(
            f"/consent/grants/{grant_id}/acknowledge", json={"signature": f"/s/ {who.user_id}"}, headers=who.headers()
        )
        assert response.status_code == expect, response.text
        return response.json()

    def ready_grant(self, who: Person = OWNER, **overrides: Any) -> dict[str, Any]:
        grant = self.create_grant(who, **overrides)
        self.verify(grant["id"])
        for beneficiary in (BEN_A, BEN_B):
            if any(b["email"] == beneficiary.email for b in grant["beneficiaries"]):
                self.acknowledge(grant["id"], beneficiary)
        return grant

    def authorize(self, who: Person, action: str, profile_id: str, purpose: str):
        return self.client.post(
            "/consent/authorize",
            json={"action": action, "profile_id": profile_id, "purpose_code": purpose},
            headers=who.headers(),
        )

    def revoke(self, grant_id: str, who: Person = OWNER, reason: str = "family decision", expect: int = 202) -> dict[str, Any]:
        response = self.client.post(f"/consent/grants/{grant_id}/revoke", json={"reason": reason}, headers=who.headers())
        assert response.status_code == expect, response.text
        return response.json()


def day(text: str) -> date:
    return date.fromisoformat(text)
