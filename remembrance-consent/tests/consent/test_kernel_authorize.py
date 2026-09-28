"""authorize(): the transactional kernel entry point (SQLite)."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.auth import Actor
from app.consent.audit import iter_events, verify_chain
from app.consent.constants import (
    BANNED_PURPOSES,
    GENERIC_PUBLIC_DENIAL,
    UNATTRIBUTED_PROFILE_ID,
    AuditEventType,
    ConsentAction,
    DenialReason,
)
from app.consent.kernel import KernelContext, PurposeViolationError, authorize, load_profile_facts
from app.consent.models import AuditEvent, AuthorizationToken
from app.consent.tokens import SqlRevocationChecker, TokenIssuer, TokenValidator
from tests.factories import OWNER_ID, seed_grant, seed_profile

OWNER = Actor(OWNER_ID, "owner@example.com")
STRANGER = Actor("user-stranger")


@pytest.fixture
def ctx(session_factory, keyring, clock):
    return KernelContext(session_factory, TokenIssuer(keyring, issuer="iss", audience="aud"), clock)


@pytest.fixture
def validator(session_factory, keyring, clock):
    return TokenValidator(
        verification_keys=keyring.verification_keys,
        issuer="iss",
        audience="aud",
        revocation=SqlRevocationChecker(session_factory),
        clock=clock,
    )


def events(session_factory):
    with session_factory() as s:
        return s.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()


def test_allowed_synthesis_issues_a_recorded_token_and_audits_it(ctx, session_factory, clock, validator):
    profile_id = seed_profile(session_factory, clock)
    grant_id = seed_grant(session_factory, clock, profile_id)

    result = authorize(ConsentAction.INITIATE_VOICE_SYNTHESIS, profile_id, OWNER, purpose_code="voice synthesis", ctx=ctx)

    assert result.allowed and result.consent_grant_id == grant_id and result.disclosed_reason is None
    verified = validator.validate(result.token, action=ConsentAction.INITIATE_VOICE_SYNTHESIS, profile_id=profile_id)
    assert verified.jti == result.jti and verified.grant_id == grant_id and verified.synthetic_disclosure_required
    with session_factory() as s:
        row = s.get(AuthorizationToken, result.jti)
        assert row.consent_grant_id == grant_id and row.revoked_at is None and row.purpose == "VOICE_SYNTHESIS"
    [event] = events(session_factory)
    assert event.id == result.audit_event_id
    assert event.event_type is AuditEventType.SYNTHESIS_REQUESTED
    assert event.payload["jti"] == str(result.jti) and event.consent_grant_id == grant_id


def test_non_synthesis_actions_audit_as_authorization_events(ctx, session_factory, clock):
    profile_id = seed_profile(session_factory, clock)
    seed_grant(session_factory, clock, profile_id)
    allowed = authorize(ConsentAction.VIEW_MEMORIAL, profile_id, STRANGER, purpose_code="MEMORIAL_VIEW", ctx=ctx)
    denied = authorize(ConsentAction.EXPORT_DATA, profile_id, STRANGER, purpose_code="DATA_PORTABILITY", ctx=ctx)
    assert allowed.allowed and not denied.allowed
    kinds = [e.event_type for e in events(session_factory)]
    assert kinds == [AuditEventType.AUTHORIZATION_GRANTED, AuditEventType.AUTHORIZATION_DENIED]


def test_denied_synthesis_is_blocked_and_audited_with_reason(ctx, session_factory, clock):
    profile_id = seed_profile(session_factory, clock)
    seed_grant(session_factory, clock, profile_id, acknowledged=("ben.a@example.com",))

    result = authorize(ConsentAction.GENERATE_SCRIPT, profile_id, OWNER, purpose_code="SCRIPT_GENERATION", ctx=ctx)

    assert not result.allowed and result.token is None and result.jti is None
    assert result.reason is DenialReason.ACKNOWLEDGMENT_QUORUM_NOT_MET
    assert result.disclosed_reason == "ACKNOWLEDGMENT_QUORUM_NOT_MET"
    [event] = events(session_factory)
    assert event.event_type is AuditEventType.SYNTHESIS_BLOCKED
    assert event.payload == {"action": "GENERATE_SCRIPT", "purpose": "SCRIPT_GENERATION", "reason": "ACKNOWLEDGMENT_QUORUM_NOT_MET"}
    with session_factory() as s:
        assert s.scalars(select(AuthorizationToken)).all() == []


def test_strangers_see_a_generic_reason_but_public_reasons_stay_precise(ctx, session_factory, clock):
    profile_id = seed_profile(session_factory, clock)
    seed_grant(session_factory, clock, profile_id, revoked=True)
    hidden = authorize(ConsentAction.VIEW_MEMORIAL, profile_id, STRANGER, purpose_code="MEMORIAL_VIEW", ctx=ctx)
    assert hidden.reason is DenialReason.GRANT_REVOKED and hidden.disclosed_reason == GENERIC_PUBLIC_DENIAL
    public = authorize(ConsentAction.VIEW_MEMORIAL, profile_id, STRANGER, purpose_code="TOURISM", ctx=ctx)
    assert public.disclosed_reason == "UNKNOWN_PURPOSE"
    owner = authorize(ConsentAction.VIEW_MEMORIAL, profile_id, OWNER, purpose_code="MEMORIAL_VIEW", ctx=ctx)
    assert owner.disclosed_reason == "GRANT_REVOKED"


def test_unknown_profile_is_audited_on_the_unattributed_chain(ctx, session_factory):
    missing = uuid4()
    result = authorize(ConsentAction.VIEW_MEMORIAL, missing, OWNER, purpose_code="MEMORIAL_VIEW", ctx=ctx)
    assert result.reason is DenialReason.PROFILE_NOT_FOUND
    [event] = events(session_factory)
    assert event.deceased_profile_id == UNATTRIBUTED_PROFILE_ID
    assert event.payload["requested_profile_id"] == str(missing)


@pytest.mark.parametrize("purpose", sorted(BANNED_PURPOSES) + ["financial", "Commercial Impersonation"])
def test_banned_purposes_raise_after_being_audited(ctx, session_factory, clock, purpose):
    profile_id = seed_profile(session_factory, clock)
    seed_grant(session_factory, clock, profile_id)

    with pytest.raises(PurposeViolationError) as caught:
        authorize(ConsentAction.VIEW_MEMORIAL, profile_id, OWNER, purpose_code=purpose, ctx=ctx)

    [event] = events(session_factory)  # committed despite the exception
    assert caught.value.audit_event_id == event.id
    assert event.event_type is AuditEventType.PURPOSE_VIOLATION_ATTEMPT
    assert event.deceased_profile_id == profile_id
    assert event.payload["detected_by"] == "kernel"
    assert event.payload["purpose_codes"] == [caught.value.purpose_code]
    with session_factory() as s:
        assert s.scalars(select(AuthorizationToken)).all() == []


def test_banned_purpose_against_unknown_profile_goes_to_the_unattributed_chain(ctx, session_factory):
    with pytest.raises(PurposeViolationError):
        authorize(ConsentAction.INITIATE_VOICE_SYNTHESIS, uuid4(), OWNER, purpose_code="PROPERTY_CLAIM", ctx=ctx)
    [event] = events(session_factory)
    assert event.deceased_profile_id == UNATTRIBUTED_PROFILE_ID and "requested_profile_id" in event.payload


def test_long_purpose_codes_are_truncated_in_the_audit(ctx, session_factory, clock):
    profile_id = seed_profile(session_factory, clock)
    result = authorize(ConsentAction.VIEW_MEMORIAL, profile_id, OWNER, purpose_code="x" * 200, ctx=ctx)
    assert len(result.purpose_code) == 64
    assert len(events(session_factory)[0].payload["purpose"]) == 64


def test_many_decisions_keep_the_chain_verifiable(ctx, session_factory, clock):
    profile_id = seed_profile(session_factory, clock)
    seed_grant(session_factory, clock, profile_id)
    for n in range(20):
        clock.advance(seconds=1)
        action = [ConsentAction.VIEW_MEMORIAL, ConsentAction.INITIATE_VOICE_SYNTHESIS][n % 2]
        purpose = ["MEMORIAL_VIEW", "VOICE_SYNTHESIS"][n % 2]
        authorize(action, profile_id, OWNER if n % 3 else STRANGER, purpose_code=purpose, ctx=ctx)
    with session_factory() as s:
        report = verify_chain(iter_events(s))
    assert report.ok and report.chains[profile_id].length == 20


def test_load_profile_facts_snapshot(session_factory, clock):
    profile_id = seed_profile(session_factory, clock)
    grant_id = seed_grant(session_factory, clock, profile_id, acknowledged=("ben.a@example.com",), required=1)
    with session_factory() as s:
        facts = load_profile_facts(s, profile_id, lock=True)
        assert load_profile_facts(s, uuid4()) is None
    [grant] = facts.grants
    assert grant.id == grant_id and grant.verified and grant.quorum.satisfied
    assert grant.acknowledged_emails == {"ben.a@example.com"}
