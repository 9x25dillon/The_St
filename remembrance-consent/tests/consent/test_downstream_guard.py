"""Acceptance criterion 1: a downstream feature cannot run without a valid,
unrevoked kernel token scoped to its action and profile."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.consent.constants import ConsentAction
from app.consent.tokens import CONSENT_TOKEN_HEADER, VerifiedAuthorization, require_consent
from app.errors import ApiError, error_response
from tests.support import OWNER


@pytest.fixture
def downstream(services):
    """A stand-in voice service: its only link to consent is the validator."""
    service = FastAPI()
    service.state.token_validator = services.validator
    service.add_exception_handler(ApiError, lambda _, exc: error_response(exc))
    ran = []

    @service.post("/voice/{profile_id}/synthesize")
    def synthesize(auth: VerifiedAuthorization = Depends(require_consent(ConsentAction.INITIATE_VOICE_SYNTHESIS))):
        ran.append(auth.jti)
        return {"synthesized": True, "disclose_as_synthetic": auth.synthetic_disclosure_required}

    @service.post("/voice/by-grant/{grant}/synthesize")
    def wrong_param(auth=Depends(require_consent(ConsentAction.INITIATE_VOICE_SYNTHESIS))):  # misconfigured route
        return {}

    with TestClient(service) as client:
        yield client, ran


def call(downstream_client, profile_id, token=None):
    headers = {CONSENT_TOKEN_HEADER: token} if token else {}
    return downstream_client.post(f"/voice/{profile_id}/synthesize", headers=headers)


def failure_of(response):
    assert response.status_code == 403, response.text
    return response.json()["error"]["failure"]


def test_feature_runs_only_with_a_valid_token(api, downstream, clock):
    client, ran = downstream
    grant = api.ready_grant()
    profile_id = grant["deceased_profile_id"]

    assert failure_of(call(client, profile_id)) == "MISSING"
    token = api.authorize(OWNER, "INITIATE_VOICE_SYNTHESIS", profile_id, "VOICE_SYNTHESIS").json()["token"]
    ok = call(client, profile_id, token)
    assert ok.status_code == 200 and ok.json() == {"synthesized": True, "disclose_as_synthetic": True}
    assert failure_of(call(client, str(uuid4()), token)) == "PROFILE_MISMATCH"

    script_token = api.authorize(OWNER, "GENERATE_SCRIPT", profile_id, "SCRIPT_GENERATION").json()["token"]
    assert failure_of(call(client, profile_id, script_token)) == "ACTION_MISMATCH"

    clock.advance(seconds=306)
    assert failure_of(call(client, profile_id, token)) == "EXPIRED"
    assert len(ran) == 1


def test_revocation_stops_in_flight_tokens(api, downstream):
    client, ran = downstream
    grant = api.ready_grant()
    token = api.authorize(OWNER, "INITIATE_VOICE_SYNTHESIS", grant["deceased_profile_id"], "VOICE_SYNTHESIS").json()["token"]
    api.revoke(grant["id"], OWNER)
    assert failure_of(call(client, grant["deceased_profile_id"], token)) == "REVOKED"
    assert ran == []


def test_misconfigured_route_fails_closed(downstream):
    client, _ = downstream
    response = client.post(f"/voice/by-grant/{uuid4()}/synthesize")
    assert response.status_code == 400 and response.json()["error"]["code"] == "BAD_PROFILE_ID"
