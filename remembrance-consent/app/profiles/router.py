"""POST /profiles/{id}/death: an administrator records a death against a
certificate digest. This is what activates a SELF_PRE_NEED grant and, in
the same instant, ends its grantor's ability to act."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from app.consent.audit import append_event
from app.consent.constants import AuditEventType
from app.consent.models import DeceasedProfile
from app.consent.schemas import DeathRecordRequest, ProfileOut
from app.deps import ActorDep, ServicesDep
from app.errors import ApiError, not_found

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.post("/{profile_id}/death", response_model=ProfileOut)
def record_death(profile_id: UUID, body: DeathRecordRequest, actor: ActorDep, services: ServicesDep) -> ProfileOut:
    if not actor.is_admin:
        raise ApiError(403, "ADMIN_REQUIRED", "only an administrator can record a death")
    with services.session_factory.begin() as session:
        profile = session.scalars(select(DeceasedProfile).where(DeceasedProfile.id == profile_id).with_for_update()).first()
        if profile is None:
            raise not_found("deceased profile")
        if profile.date_of_death is not None:
            raise ApiError(409, "DEATH_ALREADY_RECORDED", "a date of death is already recorded")
        if body.date_of_death > services.clock.now().date():
            raise ApiError(422, "DATE_OF_DEATH_IN_FUTURE", "date_of_death cannot be in the future")
        if profile.date_of_birth and body.date_of_death < profile.date_of_birth:
            raise ApiError(422, "DATE_OF_DEATH_BEFORE_BIRTH", "date_of_death cannot precede date_of_birth")
        profile.date_of_death = body.date_of_death
        session.flush()
        append_event(
            session,
            clock=services.clock,
            event_type=AuditEventType.DEATH_RECORDED,
            actor_id=actor.user_id,
            deceased_profile_id=profile.id,
            payload={"death_certificate_sha256": body.death_certificate_sha256},
        )
        return ProfileOut.model_validate(profile)
