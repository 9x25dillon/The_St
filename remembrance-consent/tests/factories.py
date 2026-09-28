"""Direct-to-database seeding for unit tests that bypass the HTTP layer."""
from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from app.clock import Clock
from app.consent.constants import GrantorType, PurposeScope
from app.consent.models import (
    AuthorizationToken,
    BeneficiaryAcknowledgment,
    ConsentGrant,
    DeceasedProfile,
    GrantBeneficiary,
)
from app.db import SessionFactory
from app.downstream.models import AudioArtifact, DeliveryStatus, ScheduledDelivery, VoiceModel
from app.successors.models import SuccessorDesignation

OWNER_ID = "user-owner"


def seed_profile(sf: SessionFactory, clock: Clock, *, died: date | None = date(2026, 1, 15)) -> UUID:
    profile_id = uuid4()
    with sf.begin() as s:
        s.add(DeceasedProfile(id=profile_id, full_name="Eleanor Example", date_of_birth=date(1942, 3, 4),
                              date_of_death=died, jurisdiction_state="US-CA", created_at=clock.now()))
    return profile_id


def seed_grant(
    sf: SessionFactory,
    clock: Clock,
    profile_id: UUID,
    *,
    grantor: str = OWNER_ID,
    grantor_type: GrantorType = GrantorType.EXECUTOR,
    scope=frozenset(PurposeScope),
    verified: bool = True,
    beneficiaries: tuple[str, ...] = ("ben.a@example.com", "ben.b@example.com"),
    acknowledged: tuple[str, ...] | None = None,
    required: int | None = None,
    revoked: bool = False,
) -> UUID:
    grant_id = uuid4()
    now = clock.now()
    acknowledged = beneficiaries if acknowledged is None else acknowledged
    with sf.begin() as s:
        s.add(ConsentGrant(
            id=grant_id, deceased_profile_id=profile_id, grantor_type=grantor_type, grantor_user_id=grantor,
            grantor_legal_name="Olivia Owner", grantor_contact_email="owner@example.com",
            legal_authority_document_url="https://documents.example/letters.pdf",
            authority_verified_at=now if verified else None, authority_verified_by="user-admin" if verified else None,
            purpose_scope=frozenset(scope), required_acknowledgments=required, is_active=not revoked, granted_at=now,
            revoked_at=now if revoked else None, revocation_reason="seeded" if revoked else None,
        ))
        s.flush()
        for email in beneficiaries:
            s.add(GrantBeneficiary(consent_grant_id=grant_id, beneficiary_name=email.split("@")[0],
                                   beneficiary_email=email, created_at=now))
        s.flush()
        for email in acknowledged:
            s.add(BeneficiaryAcknowledgment(consent_grant_id=grant_id, beneficiary_name=email.split("@")[0],
                                            beneficiary_email=email, acknowledgment_signature_hash="f" * 64,
                                            acknowledged_at=now))
    return grant_id


def seed_successor(sf: SessionFactory, clock: Clock, profile_id: UUID, user_id: str, priority: int = 1) -> None:
    with sf.begin() as s:
        s.add(SuccessorDesignation(deceased_profile_id=profile_id, successor_user_id=user_id, priority_order=priority,
                                   designated_at=clock.now(), designated_by=OWNER_ID))


def seed_token(sf: SessionFactory, clock: Clock, profile_id: UUID, grant_id: UUID | None) -> UUID:
    from datetime import timedelta

    from app.consent.constants import ConsentAction

    jti = uuid4()
    with sf.begin() as s:
        s.add(AuthorizationToken(jti=jti, consent_grant_id=grant_id, deceased_profile_id=profile_id,
                                 action=ConsentAction.INITIATE_VOICE_SYNTHESIS, purpose="VOICE_SYNTHESIS",
                                 actor_id=OWNER_ID, issued_at=clock.now(), expires_at=clock.now() + timedelta(minutes=5)))
    return jti


def seed_artifacts(sf, clock, storage, profile_id: UUID, grant_id: UUID, *, other_grant_id: UUID | None = None) -> dict[str, list[UUID]]:
    """Two voice models, three audio artifacts (one tagged with another grant
    but made from this grant's model), a scheduled and a sent delivery."""
    now = clock.now()
    ids: dict[str, list[UUID]] = {"models": [], "audio": [], "scheduled": [], "sent": []}
    with sf.begin() as s:
        for n in range(2):
            model = VoiceModel(id=uuid4(), consent_grant_id=grant_id, deceased_profile_id=profile_id,
                               storage_key=f"models/{grant_id}/{n}.bin", created_at=now)
            storage.put(model.storage_key, b"model-weights", "application/octet-stream")
            s.add(model)
            ids["models"].append(model.id)
        s.flush()
        owners = [grant_id, grant_id, other_grant_id or grant_id]
        for n, tagged in enumerate(owners):
            audio = AudioArtifact(id=uuid4(), consent_grant_id=tagged, voice_model_id=ids["models"][n % 2],
                                  storage_key=f"audio/{grant_id}/{n}.wav", created_at=now)
            storage.put(audio.storage_key, b"RIFF", "audio/wav")
            s.add(audio)
            ids["audio"].append(audio.id)
        s.flush()
        scheduled = ScheduledDelivery(id=uuid4(), consent_grant_id=grant_id, audio_artifact_id=ids["audio"][0],
                                      scheduled_for=now, status=DeliveryStatus.SCHEDULED)
        sent = ScheduledDelivery(id=uuid4(), consent_grant_id=grant_id, audio_artifact_id=ids["audio"][1],
                                 scheduled_for=now, status=DeliveryStatus.SENT)
        s.add_all([scheduled, sent])
        ids["scheduled"].append(scheduled.id)
        ids["sent"].append(sent.id)
    return ids
