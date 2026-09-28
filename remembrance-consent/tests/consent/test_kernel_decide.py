"""The pure decision function: every allow path and every deny path."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4

import pytest

from app.auth import Actor, ActorKind
from app.consent.constants import ConsentAction, DenialReason, GrantorType, PurposeScope, Role
from app.consent.kernel import Decision, GrantFacts, ProfileFacts, decide, is_deceased_actor, quorum, roles_for

A = ConsentAction
ALL_SCOPES = frozenset(PurposeScope)
NAMED = frozenset({"a@x.test", "b@x.test"})

OWNER = Actor("owner", "owner@x.test")
ADMIN = Actor("admin", "admin@x.test", is_admin=True)
BENEFICIARY = Actor("ben", "a@x.test")
SUCCESSOR = Actor("succ", "succ@x.test")
STRANGER = Actor("stranger", "s@x.test")
SERVICE = Actor("svc", kind=ActorKind.SERVICE)
SUBJECT = Actor("subject", "subject@x.test")  # a pre-need grantor


def grant(**overrides) -> GrantFacts:
    base = GrantFacts(
        id=uuid4(),
        grantor_type=GrantorType.EXECUTOR,
        grantor_user_id=OWNER.user_id,
        purpose_scope=ALL_SCOPES,
        is_active=True,
        revoked=False,
        verified=True,
        named_emails=NAMED,
        acknowledged_emails=NAMED,
        required_acknowledgments=None,
    )
    return replace(base, **overrides)


def profile(*grants: GrantFacts, died: date | None = date(2026, 1, 15), successors=frozenset({SUCCESSOR.user_id})) -> ProfileFacts:
    return ProfileFacts(id=uuid4(), date_of_death=died, successor_user_ids=frozenset(successors), grants=tuple(grants))


PRE_NEED = grant(grantor_type=GrantorType.SELF_PRE_NEED, grantor_user_id=SUBJECT.user_id)

ALLOW_CASES = [
    ("owner synthesizes", A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", OWNER, profile(grant())),
    ("owner writes script", A.GENERATE_SCRIPT, "SCRIPT_GENERATION", OWNER, profile(grant())),
    ("owner delivers", A.DELIVER_MESSAGE, "VOICE_SYNTHESIS", OWNER, profile(grant())),
    ("stranger views memorial", A.VIEW_MEMORIAL, "MEMORIAL_VIEW", STRANGER, profile(grant())),
    ("beneficiary views memorial", A.VIEW_MEMORIAL, "memorial view", BENEFICIARY, profile(grant())),
    ("successor acts under pre-need grant", A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", SUCCESSOR, profile(PRE_NEED)),
    ("owner exports", A.EXPORT_DATA, "DATA_PORTABILITY", OWNER, profile(grant())),
    ("owner exports after revocation", A.EXPORT_DATA, "DATA_PORTABILITY", OWNER, profile(grant(revoked=True, is_active=False))),
    ("admin exports", A.EXPORT_DATA, "DATA_PORTABILITY", ADMIN, profile(grant())),
    ("successor exports", A.EXPORT_DATA, "DATA_PORTABILITY", SUCCESSOR, profile()),
    ("owner erases", A.DELETE_VOICE_MODEL, "ERASURE", OWNER, profile(grant(revoked=True, is_active=False))),
    ("admin erases", A.DELETE_VOICE_MODEL, "ERASURE", ADMIN, profile()),
    ("quorum of one", A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", OWNER,
     profile(grant(required_acknowledgments=1, acknowledged_emails=frozenset({"a@x.test"})))),
    ("no beneficiaries named", A.GENERATE_SCRIPT, "SCRIPT_GENERATION", OWNER,
     profile(grant(named_emails=frozenset(), acknowledged_emails=frozenset()))),
    ("pre-need subject exports while alive", A.EXPORT_DATA, "DATA_PORTABILITY", SUBJECT, profile(PRE_NEED, died=None)),
]


@pytest.mark.parametrize("label,action,purpose,actor,facts", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_allow(label, action, purpose, actor, facts):
    decision = decide(action, purpose, actor, facts)
    assert decision.allowed, decision
    assert decision.reason is None


def test_allow_binds_the_grant_that_passed():
    failing = grant(verified=False)
    passing = grant()
    decision = decide(A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", OWNER, profile(failing, passing))
    assert decision.allowed and decision.grant_id == passing.id


def test_rights_actions_bind_the_owners_verified_grant_or_none():
    owned = grant()
    assert decide(A.EXPORT_DATA, "DATA_PORTABILITY", OWNER, profile(owned)).grant_id == owned.id
    assert decide(A.EXPORT_DATA, "DATA_PORTABILITY", ADMIN, profile(owned)).grant_id is None


DENY_CASES = [
    ("unknown purpose", A.VIEW_MEMORIAL, "HISTORICAL_RESEARCH", OWNER, profile(grant()), DenialReason.UNKNOWN_PURPOSE),
    ("banned purpose reaching decide", A.VIEW_MEMORIAL, "FINANCIAL", OWNER, profile(grant()), DenialReason.UNKNOWN_PURPOSE),
    ("narrow purpose laundered into synthesis", A.INITIATE_VOICE_SYNTHESIS, "MEMORIAL_VIEW", OWNER, profile(grant()),
     DenialReason.PURPOSE_ACTION_MISMATCH),
    ("export under a use purpose", A.EXPORT_DATA, "VOICE_SYNTHESIS", OWNER, profile(grant()), DenialReason.PURPOSE_ACTION_MISMATCH),
    ("missing profile", A.VIEW_MEMORIAL, "MEMORIAL_VIEW", OWNER, None, DenialReason.PROFILE_NOT_FOUND),
    ("service actor", A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", SERVICE, profile(grant()), DenialReason.NON_HUMAN_ACTOR),
    ("deceased subject acts", A.VIEW_MEMORIAL, "MEMORIAL_VIEW", SUBJECT, profile(PRE_NEED), DenialReason.DECEASED_CANNOT_ACT),
    ("deceased subject exports", A.EXPORT_DATA, "DATA_PORTABILITY", SUBJECT, profile(PRE_NEED), DenialReason.DECEASED_CANNOT_ACT),
    ("subject still alive", A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", SUCCESSOR, profile(PRE_NEED, died=None),
     DenialReason.SUBJECT_NOT_DECEASED),
    ("no grants", A.VIEW_MEMORIAL, "MEMORIAL_VIEW", OWNER, profile(), DenialReason.NO_GRANT),
    ("stranger synthesizes", A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", STRANGER, profile(grant()), DenialReason.ROLE_NOT_PERMITTED),
    ("beneficiary synthesizes", A.GENERATE_SCRIPT, "SCRIPT_GENERATION", BENEFICIARY, profile(grant()), DenialReason.ROLE_NOT_PERMITTED),
    ("non-successor under pre-need grant", A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", OWNER, profile(PRE_NEED),
     DenialReason.ROLE_NOT_PERMITTED),
    ("revoked grant", A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", OWNER, profile(grant(revoked=True, is_active=False)),
     DenialReason.GRANT_REVOKED),
    ("inactive grant", A.VIEW_MEMORIAL, "MEMORIAL_VIEW", STRANGER, profile(grant(is_active=False)), DenialReason.GRANT_INACTIVE),
    ("unverified authority", A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", OWNER, profile(grant(verified=False)),
     DenialReason.AUTHORITY_NOT_VERIFIED),
    ("purpose outside scope", A.GENERATE_SCRIPT, "SCRIPT_GENERATION", OWNER,
     profile(grant(purpose_scope=frozenset({PurposeScope.VOICE_SYNTHESIS}))), DenialReason.PURPOSE_NOT_IN_SCOPE),
    ("quorum missing one", A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", OWNER,
     profile(grant(acknowledged_emails=frozenset({"a@x.test"}))), DenialReason.ACKNOWLEDGMENT_QUORUM_NOT_MET),
    ("acks from outsiders don't count", A.VIEW_MEMORIAL, "MEMORIAL_VIEW", STRANGER,
     profile(grant(acknowledged_emails=frozenset({"a@x.test", "outsider@x.test"}))), DenialReason.ACKNOWLEDGMENT_QUORUM_NOT_MET),
    ("stranger exports", A.EXPORT_DATA, "DATA_PORTABILITY", STRANGER, profile(grant()), DenialReason.ROLE_NOT_PERMITTED),
    ("beneficiary exports", A.EXPORT_DATA, "DATA_PORTABILITY", BENEFICIARY, profile(grant()), DenialReason.ROLE_NOT_PERMITTED),
    ("unverified owner exports", A.EXPORT_DATA, "DATA_PORTABILITY", OWNER, profile(grant(verified=False)),
     DenialReason.ROLE_NOT_PERMITTED),
    ("stranger erases", A.DELETE_VOICE_MODEL, "ERASURE", STRANGER, profile(grant()), DenialReason.ROLE_NOT_PERMITTED),
]


@pytest.mark.parametrize("label,action,purpose,actor,facts,reason", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_deny(label, action, purpose, actor, facts, reason):
    decision = decide(action, purpose, actor, facts)
    assert not decision.allowed
    assert decision.reason is reason
    assert decision.grant_id is None


def test_every_decidable_denial_reason_is_exercised():
    covered = {case[-1] for case in DENY_CASES}
    # PURPOSE_VIOLATION is raised by authorize() before decide() runs.
    assert covered == set(DenialReason) - {DenialReason.PURPOSE_VIOLATION}


def test_every_action_has_an_allow_path():
    assert {case[1] for case in ALLOW_CASES} == set(ConsentAction)


def test_reported_reason_comes_from_the_grant_that_got_furthest():
    facts = profile(grant(revoked=True, is_active=False), grant(acknowledged_emails=frozenset()), grant(verified=False))
    assert decide(A.INITIATE_VOICE_SYNTHESIS, "VOICE_SYNTHESIS", OWNER, facts).reason is DenialReason.ACKNOWLEDGMENT_QUORUM_NOT_MET


def test_actor_relationship_flags():
    facts = profile(grant())
    assert decide(A.GENERATE_SCRIPT, "SCRIPT_GENERATION", BENEFICIARY, facts).actor_related
    assert not decide(A.GENERATE_SCRIPT, "SCRIPT_GENERATION", STRANGER, facts).actor_related
    assert decide(A.EXPORT_DATA, "DATA_PORTABILITY", SUCCESSOR, facts).actor_related


def test_roles_and_quorum_helpers():
    g = grant()
    facts = profile(g)
    assert roles_for(OWNER, facts, g) == {Role.PUBLIC, Role.OWNER}
    assert roles_for(BENEFICIARY, facts) == {Role.PUBLIC, Role.BENEFICIARY}
    assert roles_for(ADMIN, facts) == {Role.PUBLIC, Role.ADMIN}
    assert roles_for(SUCCESSOR, facts) == {Role.PUBLIC, Role.SUCCESSOR}
    assert roles_for(Actor("x"), facts) == {Role.PUBLIC}
    assert quorum(frozenset(), frozenset(), None).satisfied
    assert quorum(NAMED, frozenset({"a@x.test"}), None) == type(quorum(NAMED, NAMED, 1))(required=2, acknowledged=1)
    assert not quorum(NAMED, frozenset({"a@x.test"}), None).satisfied
    assert quorum(NAMED, frozenset(), 0).satisfied


def test_deceased_actor_detection_needs_a_recorded_death():
    assert is_deceased_actor(SUBJECT, profile(PRE_NEED))
    assert not is_deceased_actor(SUBJECT, profile(PRE_NEED, died=None))
    assert not is_deceased_actor(OWNER, profile(grant()))


def test_decision_defaults():
    assert Decision(False) == Decision(False, None, None, None, False)
    assert isinstance(grant().id, UUID)
