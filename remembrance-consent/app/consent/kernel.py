"""The consent kernel: the single authorization gate for every downstream
feature.

    authorize(action, profile_id, actor, *, purpose_code, ctx) -> AuthorizationResult

Structure:
  load_profile_facts  reads an immutable snapshot (grants FOR SHARE on
                      PostgreSQL, so a concurrent revocation waits for this
                      decision to commit, then invalidates its token)
  decide              pure function of (action, purpose, actor, facts);
                      every allow and deny path is a row in its tests
  authorize           runs decide inside one transaction: token row +
                      audit event commit together or not at all

Deny by default. Evaluation order (first failure wins):

  purpose  banned -> PurposeViolationError (audited, then raised)
           unknown -> UNKNOWN_PURPOSE
           != ACTION_PURPOSE[action] -> PURPOSE_ACTION_MISMATCH
  profile  missing -> PROFILE_NOT_FOUND
  actor    not human -> NON_HUMAN_ACTOR
           is the deceased (SELF_PRE_NEED grantor, death recorded)
             -> DECEASED_CANNOT_ACT   (death terminates agency)
  use actions (VIEW_MEMORIAL, INITIATE_VOICE_SYNTHESIS, GENERATE_SCRIPT,
  DELIVER_MESSAGE):
           subject alive -> SUBJECT_NOT_DECEASED (pre-need consent takes
             effect only on death)
           no grants -> NO_GRANT
           per grant, in order: ROLE_NOT_PERMITTED, GRANT_REVOKED,
             GRANT_INACTIVE, AUTHORITY_NOT_VERIFIED, PURPOSE_NOT_IN_SCOPE,
             ACKNOWLEDGMENT_QUORUM_NOT_MET. The first grant passing every
             check is used; otherwise the reason from the grant that got
             furthest is reported.
  rights actions (EXPORT_DATA, DELETE_VOICE_MODEL): allowed for ADMIN, a
           designated successor, or the grantor of a verified grant (active
           or revoked: access and erasure outlive consent); else
           ROLE_NOT_PERMITTED.

Who may use a grant: anyone may view a memorial; synthesis actions belong
to the grantor, except under a SELF_PRE_NEED grant, whose grantor is the
deceased, where the designated successors act instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Actor, ActorKind
from app.clock import Clock
from app.consent.audit import append_event
from app.consent.constants import (
    ACTION_PURPOSE,
    GENERIC_PUBLIC_DENIAL,
    PUBLIC_DENIAL_REASONS,
    SYNTHESIS_ACTIONS,
    UNATTRIBUTED_PROFILE_ID,
    USE_ACTIONS,
    AuditEventType,
    ConsentAction,
    DeclaredPurpose,
    DenialReason,
    GrantorType,
    PurposeScope,
    Role,
    is_banned_purpose,
    normalize_purpose,
    parse_declared_purpose,
)
from app.consent.models import AuthorizationToken, ConsentGrant, DeceasedProfile
from app.consent.tokens import TokenIssuer
from app.db import SessionFactory
from app.successors.models import SuccessorDesignation

MAX_AUDITED_PURPOSE_LENGTH = 64


class PurposeViolationError(Exception):
    """A banned purpose was requested. Raised after the attempt is audited."""

    def __init__(self, purpose_code: str, audit_event_id: int) -> None:
        super().__init__(f"purpose {purpose_code!r} is prohibited on this platform")
        self.purpose_code = purpose_code
        self.audit_event_id = audit_event_id


@dataclass(frozen=True, slots=True)
class Quorum:
    required: int
    acknowledged: int

    @property
    def satisfied(self) -> bool:
        return self.acknowledged >= self.required


def quorum(named_emails: frozenset[str], acknowledged_emails: frozenset[str], required: int | None) -> Quorum:
    """Acknowledgments count only from named beneficiaries; `required=None`
    means all of them (zero named beneficiaries is trivially satisfied)."""
    return Quorum(
        required=len(named_emails) if required is None else required,
        acknowledged=len(named_emails & acknowledged_emails),
    )


@dataclass(frozen=True, slots=True)
class GrantFacts:
    id: UUID
    grantor_type: GrantorType
    grantor_user_id: str
    purpose_scope: frozenset[PurposeScope]
    is_active: bool
    revoked: bool
    verified: bool
    named_emails: frozenset[str]
    acknowledged_emails: frozenset[str]
    required_acknowledgments: int | None

    @property
    def quorum(self) -> Quorum:
        return quorum(self.named_emails, self.acknowledged_emails, self.required_acknowledgments)

    @classmethod
    def from_row(cls, grant: ConsentGrant) -> "GrantFacts":
        return cls(
            id=grant.id,
            grantor_type=GrantorType(grant.grantor_type),
            grantor_user_id=grant.grantor_user_id,
            purpose_scope=frozenset(grant.purpose_scope),
            is_active=grant.is_active,
            revoked=grant.revoked_at is not None,
            verified=grant.authority_verified_at is not None,
            named_emails=frozenset(b.beneficiary_email for b in grant.beneficiaries),
            acknowledged_emails=frozenset(a.beneficiary_email for a in grant.acknowledgments),
            required_acknowledgments=grant.required_acknowledgments,
        )


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    id: UUID
    date_of_death: date | None
    successor_user_ids: frozenset[str]
    grants: tuple[GrantFacts, ...]  # ordered by (granted_at, id)


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: DenialReason | None = None
    grant_id: UUID | None = None
    purpose: DeclaredPurpose | None = None
    actor_related: bool = False  # actor has a relationship to this profile


def roles_for(actor: Actor, facts: ProfileFacts, grant: GrantFacts | None = None) -> frozenset[Role]:
    """Relationship of the actor to one grant (or, without one, to any grant
    on the profile)."""
    roles = {Role.PUBLIC}
    if actor.is_admin:
        roles.add(Role.ADMIN)
    if actor.user_id in facts.successor_user_ids:
        roles.add(Role.SUCCESSOR)
    for candidate in (grant,) if grant else facts.grants:
        if actor.user_id == candidate.grantor_user_id:
            roles.add(Role.OWNER)
        if actor.email is not None and actor.email in candidate.named_emails:
            roles.add(Role.BENEFICIARY)
    return frozenset(roles)


def is_deceased_actor(actor: Actor, facts: ProfileFacts) -> bool:
    return facts.date_of_death is not None and any(
        g.grantor_type is GrantorType.SELF_PRE_NEED and g.grantor_user_id == actor.user_id for g in facts.grants
    )


def may_use(action: ConsentAction, actor: Actor, grant: GrantFacts, facts: ProfileFacts) -> bool:
    if action is ConsentAction.VIEW_MEMORIAL:
        return True
    if grant.grantor_type is GrantorType.SELF_PRE_NEED:
        return actor.user_id in facts.successor_user_ids
    return actor.user_id == grant.grantor_user_id


# Stage index of each per-grant failure: higher means the grant got further.
_GRANT_STAGES: tuple[DenialReason, ...] = (
    DenialReason.ROLE_NOT_PERMITTED,
    DenialReason.GRANT_REVOKED,
    DenialReason.GRANT_INACTIVE,
    DenialReason.AUTHORITY_NOT_VERIFIED,
    DenialReason.PURPOSE_NOT_IN_SCOPE,
    DenialReason.ACKNOWLEDGMENT_QUORUM_NOT_MET,
)


def _grant_failure(action: ConsentAction, purpose: DeclaredPurpose, actor: Actor, grant: GrantFacts, facts: ProfileFacts) -> DenialReason | None:
    if not may_use(action, actor, grant, facts):
        return DenialReason.ROLE_NOT_PERMITTED
    if grant.revoked:
        return DenialReason.GRANT_REVOKED
    if not grant.is_active:
        return DenialReason.GRANT_INACTIVE
    if not grant.verified:
        return DenialReason.AUTHORITY_NOT_VERIFIED
    if PurposeScope(purpose.value) not in grant.purpose_scope:
        return DenialReason.PURPOSE_NOT_IN_SCOPE
    if not grant.quorum.satisfied:
        return DenialReason.ACKNOWLEDGMENT_QUORUM_NOT_MET
    return None


def decide(action: ConsentAction, purpose_code: str, actor: Actor, facts: ProfileFacts | None) -> Decision:
    """Pure authorization decision. Banned purposes must be screened by the
    caller (authorize does); here they would fall through as UNKNOWN_PURPOSE."""
    purpose = parse_declared_purpose(purpose_code)
    if purpose is None:
        return Decision(False, DenialReason.UNKNOWN_PURPOSE)
    if ACTION_PURPOSE[action] is not purpose:
        return Decision(False, DenialReason.PURPOSE_ACTION_MISMATCH, purpose=purpose)
    if facts is None:
        return Decision(False, DenialReason.PROFILE_NOT_FOUND, purpose=purpose)
    related = roles_for(actor, facts) != {Role.PUBLIC}
    denied = lambda reason: Decision(False, reason, purpose=purpose, actor_related=related)  # noqa: E731
    if actor.kind is not ActorKind.HUMAN:
        return denied(DenialReason.NON_HUMAN_ACTOR)
    if is_deceased_actor(actor, facts):
        return denied(DenialReason.DECEASED_CANNOT_ACT)

    if action not in USE_ACTIONS:
        if actor.is_admin or actor.user_id in facts.successor_user_ids:
            return Decision(True, grant_id=None, purpose=purpose, actor_related=True)
        for grant in facts.grants:
            if grant.verified and grant.grantor_user_id == actor.user_id:
                return Decision(True, grant_id=grant.id, purpose=purpose, actor_related=True)
        return denied(DenialReason.ROLE_NOT_PERMITTED)

    if facts.date_of_death is None:
        return denied(DenialReason.SUBJECT_NOT_DECEASED)
    if not facts.grants:
        return denied(DenialReason.NO_GRANT)
    best: DenialReason | None = None
    for grant in facts.grants:
        failure = _grant_failure(action, purpose, actor, grant, facts)
        if failure is None:
            return Decision(True, grant_id=grant.id, purpose=purpose, actor_related=related)
        if best is None or _GRANT_STAGES.index(failure) > _GRANT_STAGES.index(best):
            best = failure
    return denied(best)


def load_profile_facts(session: Session, profile_id: UUID, *, lock: bool = False) -> ProfileFacts | None:
    profile = session.get(DeceasedProfile, profile_id)
    if profile is None:
        return None
    statement = (
        select(ConsentGrant)
        .where(ConsentGrant.deceased_profile_id == profile_id)
        .order_by(ConsentGrant.granted_at, ConsentGrant.id)
    )
    if lock:
        statement = statement.with_for_update(read=True)
    grants = session.scalars(statement).all()
    successors = session.scalars(
        select(SuccessorDesignation.successor_user_id).where(SuccessorDesignation.deceased_profile_id == profile_id)
    ).all()
    return ProfileFacts(
        id=profile.id,
        date_of_death=profile.date_of_death,
        successor_user_ids=frozenset(successors),
        grants=tuple(GrantFacts.from_row(g) for g in grants),
    )


@dataclass(frozen=True)
class KernelContext:
    session_factory: SessionFactory
    issuer: TokenIssuer
    clock: Clock


@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    action: ConsentAction
    profile_id: UUID
    purpose_code: str
    reason: DenialReason | None
    consent_grant_id: UUID | None
    token: str | None
    jti: UUID | None
    expires_at: datetime | None
    audit_event_id: int
    actor_related: bool

    @property
    def disclosed_reason(self) -> str | None:
        """Reason safe to show this caller: precise for people related to the
        profile, generic for strangers (so they can't learn, e.g., that the
        family revoked consent)."""
        if self.reason is None:
            return None
        if self.actor_related or self.reason in PUBLIC_DENIAL_REASONS:
            return self.reason.value
        return GENERIC_PUBLIC_DENIAL


def _audited_purpose(purpose_code: str) -> str:
    return normalize_purpose(purpose_code)[:MAX_AUDITED_PURPOSE_LENGTH]


def record_purpose_violation(
    session: Session,
    *,
    clock: Clock,
    actor: Actor,
    purpose_codes: list[str],
    profile_id: UUID | None,
    detected_by: str,
    route: str,
) -> int:
    """Audit a banned-purpose attempt; returns the audit event id. Attempts
    naming an unknown profile go to the unattributed chain."""
    known = profile_id is not None and session.get(DeceasedProfile, profile_id) is not None
    payload: dict[str, object] = {
        "purpose_codes": sorted({_audited_purpose(code) for code in purpose_codes}),
        "detected_by": detected_by,
        "route": route,
        "actor_kind": actor.kind.value,
    }
    if not known and profile_id is not None:
        payload["requested_profile_id"] = str(profile_id)
    event = append_event(
        session,
        clock=clock,
        event_type=AuditEventType.PURPOSE_VIOLATION_ATTEMPT,
        actor_id=actor.user_id,
        deceased_profile_id=profile_id if known else UNATTRIBUTED_PROFILE_ID,
        payload=payload,
    )
    return event.id


def authorize(
    action: ConsentAction,
    profile_id: UUID,
    actor: Actor,
    *,
    purpose_code: str,
    ctx: KernelContext,
) -> AuthorizationResult:
    action = ConsentAction(action)
    if is_banned_purpose(purpose_code):
        with ctx.session_factory.begin() as session:
            event_id = record_purpose_violation(
                session,
                clock=ctx.clock,
                actor=actor,
                purpose_codes=[purpose_code],
                profile_id=profile_id,
                detected_by="kernel",
                route="kernel.authorize",
            )
        raise PurposeViolationError(_audited_purpose(purpose_code), event_id)

    with ctx.session_factory.begin() as session:
        facts = load_profile_facts(session, profile_id, lock=True)
        decision = decide(action, purpose_code, actor, facts)
        synthesis = action in SYNTHESIS_ACTIONS
        payload: dict[str, object] = {"action": action.value, "purpose": _audited_purpose(purpose_code)}
        issued = None
        if decision.allowed:
            assert decision.purpose is not None
            issued = ctx.issuer.issue(
                jti=uuid4(),
                actor_id=actor.user_id,
                action=action,
                profile_id=profile_id,
                grant_id=decision.grant_id,
                purpose=decision.purpose.value,
                now=ctx.clock.now(),
            )
            session.add(
                AuthorizationToken(
                    jti=issued.jti,
                    consent_grant_id=decision.grant_id,
                    deceased_profile_id=profile_id,
                    action=action,
                    purpose=decision.purpose.value,
                    actor_id=actor.user_id,
                    issued_at=issued.issued_at,
                    expires_at=issued.expires_at,
                )
            )
            session.flush()
            payload.update({"jti": str(issued.jti), "expires_at": issued.expires_at.isoformat()})
            event_type = AuditEventType.SYNTHESIS_REQUESTED if synthesis else AuditEventType.AUTHORIZATION_GRANTED
        else:
            assert decision.reason is not None
            payload["reason"] = decision.reason.value
            event_type = AuditEventType.SYNTHESIS_BLOCKED if synthesis else AuditEventType.AUTHORIZATION_DENIED
        if facts is None:
            payload["requested_profile_id"] = str(profile_id)
        event = append_event(
            session,
            clock=ctx.clock,
            event_type=event_type,
            actor_id=actor.user_id,
            deceased_profile_id=profile_id if facts is not None else UNATTRIBUTED_PROFILE_ID,
            consent_grant_id=decision.grant_id,
            payload=payload,
        )
        return AuthorizationResult(
            allowed=decision.allowed,
            action=action,
            profile_id=profile_id,
            purpose_code=_audited_purpose(purpose_code),
            reason=decision.reason,
            consent_grant_id=decision.grant_id,
            token=issued.token if issued else None,
            jti=issued.jti if issued else None,
            expires_at=issued.expires_at if issued else None,
            audit_event_id=event.id,
            actor_related=decision.actor_related,
        )
