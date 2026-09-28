"""POST /export/{profile_id}: full, verifiable export (GDPR Art. 15/20).

The export is itself a downstream feature of the kernel: it obtains an
EXPORT_DATA token and validates it with the same TokenValidator every other
downstream service uses before reading anything.

GET /export/download serves local-storage archives against an HMAC-signed,
expiring URL (the capability is the URL; no session is needed, so a family
member can hand the link to a lawyer or archivist).
"""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, status
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.consent.audit import append_event, format_timestamp, iter_events
from app.consent.constants import AuditEventType, ConsentAction, DeclaredPurpose
from app.consent.kernel import authorize
from app.consent.models import (
    BeneficiaryAcknowledgment,
    ConsentGrant,
    DeceasedProfile,
    RevocationRequest,
)
from app.consent.schemas import ExportResponse
from app.consent.tokens import TokenError
from app.db import is_postgres
from app.deps import ActorDep, ServicesDep
from app.downstream.models import AudioArtifact, ScheduledDelivery, VoiceModel
from app.errors import ApiError
from app.export.manifest import build_bundle
from app.storage import LocalFileStorage, StorageError
from app.successors.models import SuccessorDesignation

router = APIRouter(prefix="/export", tags=["export"])

EXPORT_PREFIX = "exports/"


def _ts(value) -> str | None:
    return format_timestamp(value) if value is not None else None


def collect_documents(session: Session, profile_id: UUID) -> dict[str, Any]:
    profile = session.get(DeceasedProfile, profile_id)
    assert profile is not None
    grants = session.scalars(
        select(ConsentGrant).where(ConsentGrant.deceased_profile_id == profile_id).order_by(ConsentGrant.granted_at, ConsentGrant.id)
    ).all()
    grant_ids = [g.id for g in grants]
    acks = session.scalars(
        select(BeneficiaryAcknowledgment)
        .where(BeneficiaryAcknowledgment.consent_grant_id.in_(grant_ids))
        .order_by(BeneficiaryAcknowledgment.acknowledged_at, BeneficiaryAcknowledgment.id)
    ).all()
    successors = session.scalars(
        select(SuccessorDesignation)
        .where(SuccessorDesignation.deceased_profile_id == profile_id)
        .order_by(SuccessorDesignation.priority_order)
    ).all()
    revocations = session.scalars(
        select(RevocationRequest).where(RevocationRequest.consent_grant_id.in_(grant_ids)).order_by(RevocationRequest.created_at)
    ).all()
    models = session.scalars(select(VoiceModel).where(VoiceModel.deceased_profile_id == profile_id).order_by(VoiceModel.created_at)).all()
    audio = session.scalars(
        select(AudioArtifact)
        .where(or_(AudioArtifact.consent_grant_id.in_(grant_ids), AudioArtifact.voice_model_id.in_([m.id for m in models])))
        .order_by(AudioArtifact.created_at)
    ).all()
    deliveries = session.scalars(
        select(ScheduledDelivery).where(ScheduledDelivery.consent_grant_id.in_(grant_ids)).order_by(ScheduledDelivery.scheduled_for)
    ).all()
    return {
        "profile.json": {
            "id": str(profile.id),
            "full_name": profile.full_name,
            "date_of_birth": profile.date_of_birth.isoformat() if profile.date_of_birth else None,
            "date_of_death": profile.date_of_death.isoformat() if profile.date_of_death else None,
            "jurisdiction_state": profile.jurisdiction_state,
            "created_at": _ts(profile.created_at),
        },
        "grants.json": [
            {
                "id": str(g.id),
                "grantor_type": g.grantor_type.value,
                "grantor_user_id": g.grantor_user_id,
                "grantor_legal_name": g.grantor_legal_name,
                "grantor_contact_email": g.grantor_contact_email,
                "legal_authority_document_url": g.legal_authority_document_url,
                "authority_verified_at": _ts(g.authority_verified_at),
                "authority_verified_by": g.authority_verified_by,
                "purpose_scope": sorted(p.value for p in g.purpose_scope),
                "required_acknowledgments": g.required_acknowledgments,
                "is_active": g.is_active,
                "granted_at": _ts(g.granted_at),
                "revoked_at": _ts(g.revoked_at),
                "revocation_reason": g.revocation_reason,
                "named_beneficiaries": [
                    {"name": b.beneficiary_name, "email": b.beneficiary_email} for b in g.beneficiaries
                ],
            }
            for g in grants
        ],
        "acknowledgments.json": [
            {
                "id": str(a.id),
                "consent_grant_id": str(a.consent_grant_id),
                "beneficiary_name": a.beneficiary_name,
                "beneficiary_email": a.beneficiary_email,
                "acknowledgment_signature_hash": a.acknowledgment_signature_hash,
                "acknowledged_at": _ts(a.acknowledged_at),
            }
            for a in acks
        ],
        "successor_designations.json": [
            {
                "id": str(s.id),
                "successor_user_id": s.successor_user_id,
                "priority_order": s.priority_order,
                "designated_at": _ts(s.designated_at),
                "designated_by": s.designated_by,
            }
            for s in successors
        ],
        "revocation_requests.json": [
            {
                "id": str(r.id),
                "consent_grant_id": str(r.consent_grant_id),
                "requested_by": r.requested_by,
                "reason": r.reason,
                "status": r.status.value,
                "created_at": _ts(r.created_at),
                "cascade_completed_at": _ts(r.cascade_completed_at),
            }
            for r in revocations
        ],
        "derived_artifacts.json": {
            "voice_models": [
                {"id": str(m.id), "consent_grant_id": str(m.consent_grant_id), "created_at": _ts(m.created_at), "deleted_at": _ts(m.deleted_at)}
                for m in models
            ],
            "audio_artifacts": [
                {"id": str(a.id), "consent_grant_id": str(a.consent_grant_id), "created_at": _ts(a.created_at), "deleted_at": _ts(a.deleted_at)}
                for a in audio
            ],
            "scheduled_deliveries": [
                {"id": str(d.id), "consent_grant_id": str(d.consent_grant_id), "scheduled_for": _ts(d.scheduled_for), "status": d.status.value}
                for d in deliveries
            ],
        },
    }


@router.post("/{profile_id}", status_code=status.HTTP_201_CREATED, response_model=ExportResponse)
def export_profile(profile_id: UUID, actor: ActorDep, services: ServicesDep) -> ExportResponse:
    result = authorize(
        ConsentAction.EXPORT_DATA,
        profile_id,
        actor,
        purpose_code=DeclaredPurpose.DATA_PORTABILITY.value,
        ctx=services.kernel,
    )
    if not result.allowed:
        if result.reason is not None and not result.actor_related:
            raise ApiError(404, "NOT_FOUND", "deceased profile not found")
        raise ApiError(403, "EXPORT_DENIED", "not authorized to export this profile", reason=result.disclosed_reason)
    try:
        authorization = services.validator.validate(result.token, action=ConsentAction.EXPORT_DATA, profile_id=profile_id)
    except TokenError as exc:  # pragma: no cover - a freshly issued token only fails if the ledger is broken
        raise ApiError(503, "EXPORT_UNAVAILABLE", "authorization could not be validated") from exc

    export_id = uuid4()
    now = services.clock.now()
    with services.session_factory() as session:
        if is_postgres(session):
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        documents = collect_documents(session, profile_id)
        events = list(iter_events(session, profile_id))
    bundle = build_bundle(
        profile_id=profile_id,
        generated_at=now,
        generated_by=actor.user_id,
        documents=documents,
        audit_events=events,
        keyring=services.keyring,
    )
    key = f"{EXPORT_PREFIX}{profile_id}/{export_id}.zip"
    services.storage.put(key, bundle.data, "application/zip")
    with services.session_factory.begin() as session:
        append_event(
            session,
            clock=services.clock,
            event_type=AuditEventType.DATA_EXPORTED,
            actor_id=actor.user_id,
            deceased_profile_id=profile_id,
            consent_grant_id=authorization.grant_id,
            payload={
                "export_id": str(export_id),
                "jti": str(authorization.jti),
                "manifest_sha256": bundle.manifest_sha256,
                "archive_sha256": hashlib.sha256(bundle.data).hexdigest(),
                "file_count": bundle.file_count,
                "audit_chain_length": bundle.manifest["audit_chain"]["length"],
            },
        )
    ttl = services.settings.export_url_ttl_seconds
    return ExportResponse(
        export_id=export_id,
        url=services.storage.signed_url(key, ttl, now),
        expires_at=now + timedelta(seconds=ttl),
        manifest_sha256=bundle.manifest_sha256,
        file_count=bundle.file_count,
    )


@router.get("/download", include_in_schema=False)
def download_export(
    services: ServicesDep,
    key: str = Query(..., max_length=1024),
    expires: int = Query(...),
    sig: str = Query(..., min_length=64, max_length=64),
) -> Response:
    storage = services.storage
    if not isinstance(storage, LocalFileStorage):
        raise ApiError(404, "NOT_FOUND", "downloads are served by the object store")
    if not key.startswith(EXPORT_PREFIX) or not storage.signer.verify(key, expires, sig, services.clock.now()):
        raise ApiError(403, "LINK_INVALID", "this download link is invalid or has expired")
    try:
        data = storage.get(key)
    except StorageError as exc:
        raise ApiError(404, "NOT_FOUND", "export not found") from exc
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="remembrance-export-{key.rsplit("/", 1)[-1]}"',
            "Cache-Control": "no-store",
        },
    )
