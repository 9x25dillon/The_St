"""POST /successors: designate who inherits rights over a profile.

Allowed: ADMIN, or the living grantor of a verified grant on the profile
(an executor naming a backup, or a pre-need subject naming who will act
for them after death). Successors can revoke, export and erase, and they
act under the profile's SELF_PRE_NEED grant once the subject has died.
"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, status
from sqlalchemy import or_, select

from app.auth import ActorKind
from app.consent.audit import append_event
from app.consent.constants import AuditEventType, Role
from app.consent.kernel import is_deceased_actor, load_profile_facts, roles_for
from app.consent.schemas import SuccessorCreate, SuccessorOut
from app.deps import ActorDep, ServicesDep
from app.errors import ApiError, not_found
from app.successors.models import SuccessorDesignation

router = APIRouter(tags=["successors"])


@router.post("/successors", status_code=status.HTTP_201_CREATED, response_model=SuccessorOut)
def designate_successor(body: SuccessorCreate, actor: ActorDep, services: ServicesDep) -> SuccessorOut:
    if actor.kind is not ActorKind.HUMAN:
        raise ApiError(403, "NON_HUMAN_ACTOR", "successors must be designated by a person")
    with services.session_factory.begin() as session:
        facts = load_profile_facts(session, body.deceased_profile_id, lock=True)
        if facts is None or roles_for(actor, facts) == {Role.PUBLIC}:
            raise not_found("deceased profile")
        if is_deceased_actor(actor, facts):
            raise ApiError(403, "DECEASED_CANNOT_ACT", "death terminates agency; this account can no longer act")
        verified_owner = any(g.verified and not g.revoked and g.grantor_user_id == actor.user_id for g in facts.grants)
        if not (actor.is_admin or verified_owner):
            raise ApiError(403, "OWNER_REQUIRED", "only the verified grantor or an administrator can designate successors")
        clash = session.scalars(
            select(SuccessorDesignation).where(
                SuccessorDesignation.deceased_profile_id == body.deceased_profile_id,
                or_(
                    SuccessorDesignation.priority_order == body.priority_order,
                    SuccessorDesignation.successor_user_id == body.successor_user_id,
                ),
            )
        ).first()
        if clash is not None:
            raise ApiError(409, "SUCCESSOR_CONFLICT", "that successor or priority is already designated for this profile")
        designation = SuccessorDesignation(
            id=uuid4(),
            deceased_profile_id=body.deceased_profile_id,
            successor_user_id=body.successor_user_id,
            priority_order=body.priority_order,
            designated_at=services.clock.now(),
            designated_by=actor.user_id,
        )
        session.add(designation)
        session.flush()
        append_event(
            session,
            clock=services.clock,
            event_type=AuditEventType.SUCCESSOR_DESIGNATED,
            actor_id=actor.user_id,
            deceased_profile_id=body.deceased_profile_id,
            payload={
                "successor_designation_id": str(designation.id),
                "successor_user_id": body.successor_user_id,
                "priority_order": body.priority_order,
            },
        )
        return SuccessorOut.model_validate(designation)
