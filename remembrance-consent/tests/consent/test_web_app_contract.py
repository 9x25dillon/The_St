"""Consumer contract: the memorial web app's kernel client against this kernel.

The web app keeps its own memorial-view checks and asks this kernel only about
voice actions, through remembrance/kernel_client.py. That client uses only the
standard library, so it is loaded here directly: a change on either side that
breaks the other fails this suite.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.consent.constants import ACTION_PURPOSE, SYNTHESIS_ACTIONS, ConsentAction
from app.consent.models import AuditEvent
from tests.support import AUTH_SECRET, OWNER, STRANGER

CLIENT_PATH = Path(__file__).resolve().parents[3] / "remembrance" / "kernel_client.py"
WRONG_KEY = "not-the-shared-identity-assertion-key-0123"


def _load_web_client():
    spec = importlib.util.spec_from_file_location("remembrance_web_kernel_client", CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


web = _load_web_client()


def audit_count(services) -> int:
    with services.session_factory() as session:
        return session.scalar(select(func.count()).select_from(AuditEvent))


@pytest.fixture
def web_app(client):
    """The web app's client, reaching this kernel in-process instead of over the network."""

    def transport(method, url, headers, body, timeout):
        response = client.request(method, urlsplit(url).path, headers=headers, content=body)
        return response.status_code, response.content

    def config(key: str):
        return web.KernelConfig(url="http://testserver", key=key.encode())

    return SimpleNamespace(
        authorize=lambda user_id, profile_id, action="INITIATE_VOICE_SYNTHESIS", key=AUTH_SECRET: web.authorize_voice(
            config(key), user_id, profile_id, action, transport=transport
        ),
        check=lambda key=AUTH_SECRET: web.check(config(key), transport=transport),
    )


def test_voice_vocabulary_matches_the_kernel():
    assert {ConsentAction(action): purpose for action, purpose in web.VOICE_ACTIONS.items()} == {
        action: ACTION_PURPOSE[action].value for action in SYNTHESIS_ACTIONS
    }


@pytest.mark.parametrize("action", sorted(web.VOICE_ACTIONS))
def test_grantor_obtains_tokens_that_downstream_services_accept(api, services, web_app, action):
    grant = api.ready_grant()
    profile_id = grant["deceased_profile_id"]

    authorization = web_app.authorize(OWNER.user_id, profile_id, action)

    verified = services.validator.validate(authorization.token, action=ConsentAction(action), profile_id=UUID(profile_id))
    assert verified.actor_id == OWNER.user_id
    assert str(verified.grant_id) == authorization.consent_grant_id == grant["id"]
    assert str(verified.jti) == authorization.jti


def test_denials_carry_exactly_what_the_kernel_discloses(api, web_app):
    grant = api.ready_grant()
    profile_id = grant["deceased_profile_id"]
    api.revoke(grant["id"])

    for person in (OWNER, STRANGER):
        disclosed = api.authorize(person, "INITIATE_VOICE_SYNTHESIS", profile_id, "VOICE_SYNTHESIS").json()["reason"]
        with pytest.raises(web.KernelDenied) as denied:
            web_app.authorize(person.user_id, profile_id)
        assert denied.value.reason == disclosed
    assert disclosed == "NOT_AUTHORIZED"


def test_an_assertion_signed_with_another_key_is_refused_before_any_decision(api, services, web_app):
    grant = api.ready_grant()
    before = audit_count(services)

    with pytest.raises(web.KernelDenied) as denied:
        web_app.authorize(OWNER.user_id, grant["deceased_profile_id"], key=WRONG_KEY)

    assert denied.value.reason == "identity_rejected"
    assert audit_count(services) == before


def test_memorial_viewing_is_never_asked_of_the_kernel(web_app):
    with pytest.raises(web.KernelDenied) as denied:
        web_app.authorize(OWNER.user_id, "0f8fad5b-d9cb-469f-a165-70867728950e", "VIEW_MEMORIAL")
    assert denied.value.reason == "unsupported_action"


def test_check_confirms_the_shared_key_and_records_nothing(services, web_app):
    before = audit_count(services)

    web_app.check()
    with pytest.raises(web.KernelDenied) as denied:
        web_app.check(key=WRONG_KEY)

    assert denied.value.reason == "identity_rejected"
    assert audit_count(services) == before
