"""Revocation cascade.

Revoking a grant is two-phase:

  immediate (inside POST /revoke's transaction)
      grant inactive + revoked_at, every token issued under it revoked,
      revocation_request PENDING, CONSENT_REVOKED audited.
  eventual (this module, via Celery `cascade_revocation`)
      scheduled deliveries cancelled, audio artifacts and voice models
      deleted from object storage and tombstoned, MODEL_DELETED audited per
      artifact, request COMPLETE.

Idempotence and retry safety:
  - every step selects only work not yet done (status SCHEDULED,
    deleted_at IS NULL, revoked_at IS NULL), so a re-run resumes where a
    failed run stopped and a finished run is a no-op;
  - each artifact is handled in its own transaction holding its row lock:
    object delete -> tombstone -> audit, then commit. A crash after the
    object delete but before commit leaves the row live; the retry deletes
    again (delete is idempotent) and tombstones once, so MODEL_DELETED is
    emitted exactly once per artifact;
  - the request is marked COMPLETE only after a final check finds nothing
    left, including artifacts a downstream writer raced in mid-cascade;
  - revocation_request doubles as a transactional outbox: the sweeper
    re-dispatches anything not COMPLETE and stale, covering lost enqueues.

"Derived artifacts" include audio made from the grant's voice models and
deliveries of that audio, even when tagged with a different grant.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.clock import Clock
from app.consent.audit import append_event
from app.consent.constants import AuditEventType, RevocationStatus
from app.consent.models import AuthorizationToken, ConsentGrant, RevocationRequest
from app.db import SessionFactory
from app.downstream.models import AudioArtifact, DeliveryStatus, ScheduledDelivery, VoiceModel
from app.storage import ObjectStorage

log = logging.getLogger(__name__)

CASCADE_ACTOR = "system:revocation-cascade"
MAX_ERROR_LENGTH = 2000


class CascadeError(Exception):
    """Transient: retrying may succeed."""


class PermanentCascadeError(CascadeError):
    """Retrying cannot succeed (unknown grant, grant not revoked)."""


class CascadeDispatcher(Protocol):
    def dispatch(self, consent_grant_id: UUID) -> None: ...


@dataclass(frozen=True)
class CascadeReport:
    consent_grant_id: str
    already_complete: bool = False
    tokens_revoked: int = 0
    deliveries_cancelled: int = 0
    audio_artifacts_deleted: int = 0
    voice_models_deleted: int = 0

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def revoke_grant_tokens(session: Session, consent_grant_id: UUID, now: datetime) -> int:
    result = session.execute(
        update(AuthorizationToken)
        .where(AuthorizationToken.consent_grant_id == consent_grant_id, AuthorizationToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return int(result.rowcount or 0)


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _grant_models(consent_grant_id: UUID):
    return select(VoiceModel.id).where(VoiceModel.consent_grant_id == consent_grant_id)


def _audio_scope(consent_grant_id: UUID):
    return or_(
        AudioArtifact.consent_grant_id == consent_grant_id,
        AudioArtifact.voice_model_id.in_(_grant_models(consent_grant_id)),
    )


def _delivery_scope(consent_grant_id: UUID):
    return or_(
        ScheduledDelivery.consent_grant_id == consent_grant_id,
        ScheduledDelivery.audio_artifact_id.in_(select(AudioArtifact.id).where(_audio_scope(consent_grant_id))),
    )


def _remaining(session: Session, consent_grant_id: UUID) -> dict[str, int]:
    count = lambda statement: int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)  # noqa: E731
    return {
        "tokens": count(
            select(AuthorizationToken.jti).where(
                AuthorizationToken.consent_grant_id == consent_grant_id, AuthorizationToken.revoked_at.is_(None)
            )
        ),
        "deliveries": count(
            select(ScheduledDelivery.id).where(
                _delivery_scope(consent_grant_id), ScheduledDelivery.status == DeliveryStatus.SCHEDULED
            )
        ),
        "audio_artifacts": count(
            select(AudioArtifact.id).where(_audio_scope(consent_grant_id), AudioArtifact.deleted_at.is_(None))
        ),
        "voice_models": count(
            select(VoiceModel.id).where(VoiceModel.consent_grant_id == consent_grant_id, VoiceModel.deleted_at.is_(None))
        ),
    }


class _Cascade:
    def __init__(self, consent_grant_id: UUID, session_factory: SessionFactory, storage: ObjectStorage, clock: Clock) -> None:
        self.grant_id = consent_grant_id
        self.session_factory = session_factory
        self.storage = storage
        self.clock = clock
        self.profile_id: UUID | None = None
        self.request_id: UUID | None = None

    def claim(self) -> bool:
        """Mark the request PROCESSING. False when it is already COMPLETE."""
        with self.session_factory.begin() as session:
            grant = session.get(ConsentGrant, self.grant_id)
            if grant is None:
                raise PermanentCascadeError(f"unknown consent grant {self.grant_id}")
            if grant.revoked_at is None:
                raise PermanentCascadeError(f"consent grant {self.grant_id} is not revoked")
            self.profile_id = grant.deceased_profile_id
            now = self.clock.now()
            request = session.scalars(
                select(RevocationRequest).where(RevocationRequest.consent_grant_id == self.grant_id).with_for_update()
            ).first()
            if request is None:  # revoked out of band, e.g. by an operator script
                request = RevocationRequest(
                    consent_grant_id=self.grant_id,
                    requested_by=CASCADE_ACTOR,
                    reason=grant.revocation_reason or "revoked",
                    status=RevocationStatus.PENDING,
                    created_at=now,
                    updated_at=now,
                    attempts=0,
                )
                session.add(request)
                session.flush()
            self.request_id = request.id
            if request.status is RevocationStatus.COMPLETE:
                return False
            request.status = RevocationStatus.PROCESSING
            request.attempts += 1
            request.updated_at = now
            request.last_error = None
            return True

    def cancel_deliveries(self) -> int:
        with self.session_factory() as session:
            ids = session.scalars(
                select(ScheduledDelivery.id)
                .where(_delivery_scope(self.grant_id), ScheduledDelivery.status == DeliveryStatus.SCHEDULED)
                .order_by(ScheduledDelivery.id)
            ).all()
        cancelled = 0
        for delivery_id in ids:
            with self.session_factory.begin() as session:
                delivery = session.scalars(
                    select(ScheduledDelivery)
                    .where(ScheduledDelivery.id == delivery_id, ScheduledDelivery.status == DeliveryStatus.SCHEDULED)
                    .with_for_update()
                ).first()
                if delivery is None:
                    continue
                delivery.status = DeliveryStatus.CANCELLED
                delivery.cancelled_at = self.clock.now()
                self._audit(session, AuditEventType.DELIVERY_CANCELLED, {"delivery_id": str(delivery_id)})
                cancelled += 1
        return cancelled

    def delete_artifacts(self, model: type[AudioArtifact] | type[VoiceModel], kind: str, scope) -> int:
        with self.session_factory() as session:
            ids = session.scalars(select(model.id).where(scope, model.deleted_at.is_(None)).order_by(model.id)).all()
        deleted = 0
        for artifact_id in ids:
            with self.session_factory.begin() as session:
                artifact = session.scalars(
                    select(model).where(model.id == artifact_id, model.deleted_at.is_(None)).with_for_update()
                ).first()
                if artifact is None:
                    continue
                key = artifact.storage_key
                if key:
                    self.storage.delete(key)  # idempotent; raises on transport failure -> retry
                artifact.storage_key = None
                artifact.deleted_at = self.clock.now()
                self._audit(
                    session,
                    AuditEventType.MODEL_DELETED,
                    {
                        "artifact_kind": kind,
                        "artifact_id": str(artifact_id),
                        "storage_key_sha256": _key_digest(key) if key else None,
                        "reason": "CONSENT_REVOKED",
                    },
                )
                deleted += 1
        return deleted

    def finish(self, report: CascadeReport) -> None:
        with self.session_factory.begin() as session:
            left = _remaining(session, self.grant_id)
            if any(left.values()):
                raise CascadeError(f"cascade incomplete, will retry: {left}")
            request = session.scalars(
                select(RevocationRequest).where(RevocationRequest.id == self.request_id).with_for_update()
            ).one()
            now = self.clock.now()
            request.status = RevocationStatus.COMPLETE
            request.cascade_completed_at = now
            request.updated_at = now
            self._audit(
                session,
                AuditEventType.REVOCATION_CASCADE_COMPLETED,
                {
                    "revocation_request_id": str(request.id),
                    "attempts": request.attempts,
                    "tokens_revoked": report.tokens_revoked,
                    "deliveries_cancelled": report.deliveries_cancelled,
                    "audio_artifacts_deleted": report.audio_artifacts_deleted,
                    "voice_models_deleted": report.voice_models_deleted,
                },
            )

    def record_failure(self, exc: BaseException) -> None:
        with self.session_factory.begin() as session:
            request = session.get(RevocationRequest, self.request_id)
            if request is not None and request.status is not RevocationStatus.COMPLETE:
                request.last_error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
                request.updated_at = self.clock.now()

    def _audit(self, session: Session, event_type: AuditEventType, payload: dict[str, object]) -> None:
        assert self.profile_id is not None
        append_event(
            session,
            clock=self.clock,
            event_type=event_type,
            actor_id=CASCADE_ACTOR,
            deceased_profile_id=self.profile_id,
            consent_grant_id=self.grant_id,
            payload=payload,
        )


def run_cascade(
    consent_grant_id: UUID, *, session_factory: SessionFactory, storage: ObjectStorage, clock: Clock
) -> CascadeReport:
    cascade = _Cascade(consent_grant_id, session_factory, storage, clock)
    if not cascade.claim():
        return CascadeReport(str(consent_grant_id), already_complete=True)
    try:
        with session_factory.begin() as session:
            tokens = revoke_grant_tokens(session, consent_grant_id, clock.now())
        report = CascadeReport(
            str(consent_grant_id),
            tokens_revoked=tokens,
            deliveries_cancelled=cascade.cancel_deliveries(),
            audio_artifacts_deleted=cascade.delete_artifacts(AudioArtifact, "AUDIO_ARTIFACT", _audio_scope(consent_grant_id)),
            voice_models_deleted=cascade.delete_artifacts(
                VoiceModel, "VOICE_MODEL", VoiceModel.consent_grant_id == consent_grant_id
            ),
        )
        cascade.finish(report)
    except Exception as exc:
        log.warning("revocation cascade for grant %s failed: %s", consent_grant_id, exc)
        cascade.record_failure(exc)
        raise
    log.info("revocation cascade for grant %s complete: %s", consent_grant_id, report.to_json())
    return report


def sweep_stale_revocations(
    *, session_factory: SessionFactory, clock: Clock, dispatcher: CascadeDispatcher, stale_after_seconds: int
) -> list[UUID]:
    """Re-dispatch every revocation not COMPLETE and untouched for a while."""
    cutoff = clock.now() - timedelta(seconds=stale_after_seconds)
    with session_factory() as session:
        grant_ids = session.scalars(
            select(RevocationRequest.consent_grant_id)
            .where(RevocationRequest.status != RevocationStatus.COMPLETE, RevocationRequest.updated_at < cutoff)
            .order_by(RevocationRequest.updated_at)
        ).all()
    for grant_id in grant_ids:
        dispatcher.dispatch(grant_id)
    return list(grant_ids)


class InlineDispatcher:
    """Runs the cascade in-process (development and tests). Failures are
    logged, not raised: the revocation itself already committed, and the
    sweeper will retry."""

    def __init__(self, runner: Callable[[UUID], CascadeReport]) -> None:
        self._runner = runner

    def dispatch(self, consent_grant_id: UUID) -> None:
        try:
            self._runner(consent_grant_id)
        except Exception:
            log.exception("inline cascade for grant %s failed; the sweeper will retry", consent_grant_id)


class CeleryDispatcher:
    def dispatch(self, consent_grant_id: UUID) -> None:
        from app.worker import cascade_revocation

        cascade_revocation.apply_async(args=[str(consent_grant_id)])
