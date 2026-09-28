from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.consent.models import enum_column
from app.db import Base
from app.types import UTCDateTime


class DeliveryStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    SENT = "SENT"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class VoiceModel(Base):
    __tablename__ = "voice_model"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    consent_grant_id: Mapped[UUID] = mapped_column(ForeignKey("consent_grant.id", ondelete="RESTRICT"), index=True)
    deceased_profile_id: Mapped[UUID] = mapped_column(ForeignKey("deceased_profile.id", ondelete="RESTRICT"))
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # A tombstoned row keeps no pointer to biometric data.
    __table_args__ = (CheckConstraint("deleted_at IS NULL OR storage_key IS NULL", name="tombstone_has_no_key"),)


class AudioArtifact(Base):
    __tablename__ = "audio_artifact"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    consent_grant_id: Mapped[UUID] = mapped_column(ForeignKey("consent_grant.id", ondelete="RESTRICT"), index=True)
    voice_model_id: Mapped[UUID | None] = mapped_column(ForeignKey("voice_model.id", ondelete="RESTRICT"))
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (CheckConstraint("deleted_at IS NULL OR storage_key IS NULL", name="tombstone_has_no_key"),)


class ScheduledDelivery(Base):
    __tablename__ = "scheduled_delivery"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    consent_grant_id: Mapped[UUID] = mapped_column(ForeignKey("consent_grant.id", ondelete="RESTRICT"), index=True)
    audio_artifact_id: Mapped[UUID | None] = mapped_column(ForeignKey("audio_artifact.id", ondelete="RESTRICT"))
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[DeliveryStatus] = mapped_column(enum_column(DeliveryStatus, "delivery_status"))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint("(status = 'CANCELLED') = (cancelled_at IS NOT NULL)", name="cancellation_timestamp"),
    )
