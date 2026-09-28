"""Consent endpoints. Every write commits together with its audit event.

Access rules (roles are relationships resolved per request):
  POST /consent/grants                 any human actor; grantor becomes OWNER
  POST /consent/grants/{id}/verify     ADMIN, never the grant's own grantor
  POST /consent/grants/{id}/acknowledge  BENEFICIARY named on the grant
  POST /consent/grants/{id}/revoke     OWNER, BENEFICIARY, SUCCESSOR, ADMIN
  GET  /consent/grants/{id}            OWNER, ADMIN
  GET  /consent/grants/{id}/audit      OWNER, BENEFICIARY, SUCCESSOR, ADMIN
  POST /consent/authorize              any actor; the kernel decides
Callers without a relationship get 404, so grant ids can't be probed.
"""
from __future__ import annotations

import hashlib
import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Actor, ActorKind
from app.consent import acknowledgments
from app.consent.audit import EventRecord, append_event, iter_events, verify_chain
from app.consent.cascade import revoke_grant_tokens
from app.consent.constants import (
    ESTATE_GRANTOR_TYPES,
    AuditEventType,
    GrantorType,
    RevocationStatus,
    Role,
)
from app.consent.kernel import (
    GrantFacts,
    ProfileFacts,
    PurposeViolationError,
    authorize,
    is_deceased_actor,
    load_profile_facts,
    roles_for,
)
from app.consent.models import (
    AuditEvent,
    BeneficiaryAcknowledgment,
    ConsentGrant,
    DeceasedProfile,
    GrantBeneficiary,
    RevocationRequest,
)
from app.consent.schemas import (
    AcknowledgeRequest,
    AcknowledgeResponse,
    AuditEventOut,
    AuditPage,
    AuthorizeRequest,
    AuthorizeResponse,
    BeneficiaryStatus,
    ChainStatus,
    GrantCreate,
    GrantOut,
    Quorum,
    RevokeRequest,
    RevokeResponse,
    VerifyRequest,
)
from app.deps import ActorDep, ServicesDep
from app.errors import ApiError, not_found

log = logging.getLogger(__name__)
router = APIRouter(prefix="/consent", tags=["consent"])

READ_ROLES = frozenset({Role.OWNER, Role.ADMIN})
AUDIT_ROLES = frozenset({Role.OWNER, Role.BENEFICIARY, Role.SUCCESSOR, Role.ADMIN})
REVOKE_ROLES = AUDIT_ROLES
MAX_AUDIT_PAGE = 500


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def grant_out(grant: ConsentGrant) -> GrantOut:
    acknowledged = {a.beneficiary_email: a.acknowledged_at for a in grant.acknowledgments}
    facts = GrantFacts.from_row(grant)
    q = facts.quorum
    return GrantOut(
        id=grant.id,
        deceased_profile_id=grant.deceased_profile_id,
        grantor_type=grant.grantor_type,
        grantor_user_id=grant.grantor_user_id,
        grantor_legal_name=grant.grantor_legal_name,
        grantor_contact_email=grant.grantor_contact_email,
        legal_authority_document_url=grant.legal_authority_document_url,
        authority_verified_at=grant.authority_verified_at,
        authority_verified_by=grant.authority_verified_by,
        purpose_scope=sorted(grant.purpose_scope),
        required_acknowledgments=grant.required_acknowledgments,
        is_active=grant.is_active,
        granted_at=grant.granted_at,
        revoked_at=grant.revoked_at,
        revocation_reason=grant.revocation_reason,
        beneficiaries=[
            BeneficiaryStatus(
                name=b.beneficiary_name, email=b.beneficiary_email, acknowledged_at=acknowledged.get(b.beneficiary_email)
            )
            for b in grant.beneficiaries
        ],
        quorum=Quorum(required=q.required, acknowledged=q.acknowledged, satisfied=q.satisfied),
    )


def _grant_and_facts(session: Session, grant_id: UUID, *, lock: bool = False) -> tuple[ConsentGrant, ProfileFacts]:
    statement = select(ConsentGrant).where(ConsentGrant.id == grant_id)
    if lock:
        statement = statement.with_for_update()
    grant = session.scalars(statement).first()
    if grant is None:
        raise not_found("consent grant")
    facts = load_profile_facts(session, grant.deceased_profile_id)
    assert facts is not None  # FK guarantees the profile
    return grant, facts


def _require(actor: Actor, facts: ProfileFacts, grant: ConsentGrant, allowed: frozenset[Role]) -> frozenset[Role]:
    roles = roles_for(actor, facts, GrantFacts.from_row(grant))
    if not roles & allowed:
        raise not_found("consent grant")
    if is_deceased_actor(actor, facts):
        raise ApiError(403, "DECEASED_CANNOT_ACT", "death terminates agency; this account can no longer act")
    return roles


def _require_human(actor: Actor) -> None:
    if actor.kind is not ActorKind.HUMAN:
        raise ApiError(403, "NON_HUMAN_ACTOR", "consent decisions must be made by a person")


@router.post("/grants", status_code=status.HTTP_201_CREATED, response_model=GrantOut)
def create_grant(body: GrantCreate, actor: ActorDep, services: ServicesDep) -> GrantOut:
    _require_human(actor)
    now = services.clock.now()
    with services.session_factory.begin() as session:
        profile_created = body.deceased_profile is not None
        if body.deceased_profile is not None:
            spec = body.deceased_profile
            if spec.date_of_death and spec.date_of_death > now.date():
                raise ApiError(422, "DATE_OF_DEATH_IN_FUTURE", "date_of_death cannot be in the future")
            profile = DeceasedProfile(
                id=uuid4(),
                full_name=spec.full_name,
                date_of_birth=spec.date_of_birth,
                date_of_death=spec.date_of_death,
                jurisdiction_state=spec.jurisdiction_state,
                created_at=now,
            )
        else:
            profile = session.get(DeceasedProfile, body.deceased_profile_id)
            if profile is None:
                raise not_found("deceased profile")

        # Principle 1: consent comes from the estate after death, or from
        # the person themselves before it. Never the reverse.
        if body.grantor_type is GrantorType.SELF_PRE_NEED and profile.date_of_death is not None:
            raise ApiError(422, "PRE_NEED_AFTER_DEATH", "pre-need consent cannot be granted after death")
        if body.grantor_type in ESTATE_GRANTOR_TYPES and profile.date_of_death is None:
            raise ApiError(422, "ESTATE_BEFORE_DEATH", "estate consent requires a recorded date_of_death")

        if profile_created:
            session.add(profile)
            session.flush()
        else:
            facts = load_profile_facts(session, profile.id)
            assert facts is not None
            if is_deceased_actor(actor, facts):
                raise ApiError(403, "DECEASED_CANNOT_ACT", "death terminates agency; this account can no longer act")
            if body.grantor_type is GrantorType.SELF_PRE_NEED and any(
                g.grantor_type is GrantorType.SELF_PRE_NEED and g.grantor_user_id != actor.user_id for g in facts.grants
            ):
                raise ApiError(409, "PRE_NEED_SUBJECT_CONFLICT", "another account already holds pre-need consent for this profile")

        grant = ConsentGrant(
            id=uuid4(),
            deceased_profile_id=profile.id,
            grantor_type=body.grantor_type,
            grantor_user_id=actor.user_id,
            grantor_legal_name=body.grantor_legal_name,
            grantor_contact_email=body.grantor_contact_email,
            legal_authority_document_url=body.legal_authority_document_url,
            purpose_scope=frozenset(body.purpose_scope),
            required_acknowledgments=body.required_acknowledgments,
            is_active=True,
            granted_at=now,
        )
        session.add(grant)
        session.flush()
        for beneficiary in body.beneficiaries:
            session.add(
                GrantBeneficiary(
                    consent_grant_id=grant.id,
                    beneficiary_name=beneficiary.name,
                    beneficiary_email=beneficiary.email,
                    created_at=now,
                )
            )
        session.flush()
        if profile_created:
            append_event(
                session,
                clock=services.clock,
                event_type=AuditEventType.PROFILE_CREATED,
                actor_id=actor.user_id,
                deceased_profile_id=profile.id,
                payload={"jurisdiction_state": profile.jurisdiction_state, "death_recorded": profile.date_of_death is not None},
            )
        append_event(
            session,
            clock=services.clock,
            event_type=AuditEventType.CONSENT_CREATED,
            actor_id=actor.user_id,
            deceased_profile_id=profile.id,
            consent_grant_id=grant.id,
            payload={
                "grantor_type": body.grantor_type.value,
                "purpose_scope": sorted(p.value for p in body.purpose_scope),
                "beneficiary_count": len(body.beneficiaries),
                "required_acknowledgments": body.required_acknowledgments,
                "legal_authority_document_url_sha256": _sha256(body.legal_authority_document_url),
            },
        )
        session.refresh(grant)
        return grant_out(grant)


@router.post("/grants/{grant_id}/verify", response_model=GrantOut)
def verify_authority(grant_id: UUID, body: VerifyRequest, actor: ActorDep, services: ServicesDep) -> GrantOut:
    if not actor.is_admin:
        raise ApiError(403, "ADMIN_REQUIRED", "only an administrator can verify legal authority")
    _require_human(actor)
    with services.session_factory.begin() as session:
        grant, _ = _grant_and_facts(session, grant_id, lock=True)
        if grant.grantor_user_id == actor.user_id:
            raise ApiError(403, "SELF_VERIFICATION", "an administrator cannot verify their own grant")
        if grant.revoked_at is not None:
            raise ApiError(409, "GRANT_REVOKED", "a revoked grant cannot be verified")
        if grant.authority_verified_at is not None:
            raise ApiError(409, "ALREADY_VERIFIED", "legal authority is already verified")
        grant.authority_verified_at = services.clock.now()
        grant.authority_verified_by = actor.user_id
        session.flush()
        append_event(
            session,
            clock=services.clock,
            event_type=AuditEventType.AUTHORITY_VERIFIED,
            actor_id=actor.user_id,
            deceased_profile_id=grant.deceased_profile_id,
            consent_grant_id=grant.id,
            payload={"document_sha256": body.document_sha256},
        )
        return grant_out(grant)


@router.post("/grants/{grant_id}/acknowledge", status_code=status.HTTP_201_CREATED, response_model=AcknowledgeResponse)
def acknowledge(grant_id: UUID, body: AcknowledgeRequest, actor: ActorDep, services: ServicesDep) -> AcknowledgeResponse:
    _require_human(actor)
    with services.session_factory.begin() as session:
        grant, facts = _grant_and_facts(session, grant_id)
        roles = _require(actor, facts, grant, frozenset({Role.BENEFICIARY}))
        assert Role.BENEFICIARY in roles and actor.email is not None
        if grant.revoked_at is not None:
            raise ApiError(409, "GRANT_REVOKED", "a revoked grant cannot be acknowledged")
        if any(a.beneficiary_email == actor.email for a in grant.acknowledgments):
            raise ApiError(409, "ALREADY_ACKNOWLEDGED", "this beneficiary already acknowledged the grant")
        named = next(b for b in grant.beneficiaries if b.beneficiary_email == actor.email)
        statement = acknowledgments.statement_for(grant, actor.email)
        now = services.clock.now()
        ack = BeneficiaryAcknowledgment(
            consent_grant_id=grant.id,
            beneficiary_name=named.beneficiary_name,
            beneficiary_email=actor.email,
            acknowledgment_signature_hash=acknowledgments.signature_hash(statement, body.signature),
            acknowledged_at=now,
        )
        session.add(ack)
        session.flush()
        session.expire(grant, ["acknowledgments"])
        q = GrantFacts.from_row(grant).quorum
        append_event(
            session,
            clock=services.clock,
            event_type=AuditEventType.BENEFICIARY_ACKNOWLEDGED,
            actor_id=actor.user_id,
            deceased_profile_id=grant.deceased_profile_id,
            consent_grant_id=grant.id,
            payload={
                "acknowledgment_id": str(ack.id),
                "grant_beneficiary_id": str(named.id),
                "statement_sha256": acknowledgments.statement_digest(statement),
                "quorum_required": q.required,
                "quorum_acknowledged": q.acknowledged,
            },
        )
        return AcknowledgeResponse(
            acknowledgment_id=ack.id,
            consent_grant_id=grant.id,
            statement=statement,
            acknowledgment_signature_hash=ack.acknowledgment_signature_hash,
            acknowledged_at=now,
            quorum=Quorum(required=q.required, acknowledged=q.acknowledged, satisfied=q.satisfied),
        )


@router.post("/grants/{grant_id}/revoke", status_code=status.HTTP_202_ACCEPTED, response_model=RevokeResponse)
def revoke(grant_id: UUID, body: RevokeRequest, actor: ActorDep, services: ServicesDep) -> RevokeResponse:
    _require_human(actor)
    with services.session_factory.begin() as session:
        grant, facts = _grant_and_facts(session, grant_id, lock=True)
        roles = _require(actor, facts, grant, REVOKE_ROLES)
        existing = session.scalars(select(RevocationRequest).where(RevocationRequest.consent_grant_id == grant.id)).first()
        if grant.revoked_at is not None and existing is not None:
            response = RevokeResponse(
                consent_grant_id=grant.id,
                revoked_at=grant.revoked_at,
                revocation_request_id=existing.id,
                status=existing.status.value,
                tokens_invalidated=0,
            )
            redispatch = existing.status is not RevocationStatus.COMPLETE
        else:
            now = services.clock.now()
            grant.is_active = False
            grant.revoked_at = now
            grant.revocation_reason = body.reason
            session.flush()
            invalidated = revoke_grant_tokens(session, grant.id, now)
            request = RevocationRequest(
                consent_grant_id=grant.id,
                requested_by=actor.user_id,
                reason=body.reason,
                status=RevocationStatus.PENDING,
                created_at=now,
                updated_at=now,
                attempts=0,
            )
            session.add(request)
            session.flush()
            append_event(
                session,
                clock=services.clock,
                event_type=AuditEventType.CONSENT_REVOKED,
                actor_id=actor.user_id,
                deceased_profile_id=grant.deceased_profile_id,
                consent_grant_id=grant.id,
                payload={
                    "revocation_request_id": str(request.id),
                    "revoked_by_roles": sorted(r.value for r in roles - {Role.PUBLIC}),
                    "tokens_invalidated": invalidated,
                    "reason_sha256": _sha256(body.reason),
                },
            )
            response = RevokeResponse(
                consent_grant_id=grant.id,
                revoked_at=now,
                revocation_request_id=request.id,
                status=request.status.value,
                tokens_invalidated=invalidated,
            )
            redispatch = True
    if redispatch:
        # After commit: the task must see the revocation. A failed enqueue is
        # recovered by the sweeper; the revocation itself already holds.
        try:
            services.dispatcher.dispatch(grant_id)
        except Exception:
            log.exception("could not enqueue revocation cascade for grant %s; the sweeper will retry", grant_id)
    return response


@router.get("/grants/{grant_id}", response_model=GrantOut)
def read_grant(grant_id: UUID, actor: ActorDep, services: ServicesDep) -> GrantOut:
    with services.session_factory() as session:
        grant, facts = _grant_and_facts(session, grant_id)
        _require(actor, facts, grant, READ_ROLES)
        return grant_out(grant)


@router.get("/grants/{grant_id}/audit", response_model=AuditPage)
def read_audit(
    grant_id: UUID,
    actor: ActorDep,
    services: ServicesDep,
    after_id: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=MAX_AUDIT_PAGE),
) -> AuditPage:
    with services.session_factory() as session:
        grant, facts = _grant_and_facts(session, grant_id)
        _require(actor, facts, grant, AUDIT_ROLES)
        rows = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.consent_grant_id == grant.id, AuditEvent.id > after_id)
            .order_by(AuditEvent.id)
            .limit(limit + 1)
        ).all()
        report = verify_chain(iter_events(session, grant.deceased_profile_id))
        chain = report.chains.get(grant.deceased_profile_id)
        page = rows[:limit]
        return AuditPage(
            events=[AuditEventOut.model_validate(EventRecord.from_row(r), from_attributes=True) for r in page],
            next_after_id=page[-1].id if len(rows) > limit else None,
            chain=ChainStatus(
                verified=report.ok,
                length=chain.length if chain else 0,
                head_hash=chain.head_hash if chain else "0" * 64,
                errors=len(report.errors),
            ),
        )


@router.post(
    "/authorize",
    response_model=AuthorizeResponse,
    responses={403: {"model": AuthorizeResponse, "description": "denied; `reason` says why when disclosable"}},
)
def authorize_action(body: AuthorizeRequest, actor: ActorDep, services: ServicesDep) -> JSONResponse:
    try:
        result = authorize(body.action, body.profile_id, actor, purpose_code=body.purpose_code, ctx=services.kernel)
    except PurposeViolationError as exc:
        raise ApiError(
            403,
            "PURPOSE_VIOLATION",
            "financial, property, legal-representation and commercial purposes are prohibited",
            audit_event_id=exc.audit_event_id,
        ) from exc
    response = AuthorizeResponse(
        allowed=result.allowed,
        action=result.action,
        profile_id=result.profile_id,
        consent_grant_id=result.consent_grant_id if result.allowed else None,
        token=result.token,
        token_type="consent+jwt" if result.token else None,
        jti=result.jti,
        expires_at=result.expires_at,
        reason=result.disclosed_reason,
    )
    return JSONResponse(
        response.model_dump(mode="json"),
        status_code=status.HTTP_200_OK if result.allowed else status.HTTP_403_FORBIDDEN,
        headers={"Cache-Control": "no-store"},
    )
