"""Consent tables.

State invariants enforced by constraints (here and in the migration):
- revoked_at set  =>  is_active false               (revocation is terminal)
- authority_verified_at and authority_verified_by are set together
- revocation_reason only on revoked grants
- acknowledgments only from beneficiaries named on the grant (composite FK)
- the audit chain of a profile is linear: (deceased_profile_id,
  previous_hash) is unique, so two events can never claim the same parent
PostgreSQL additionally rejects UPDATE/DELETE on audit_event (role grants +
trigger) and edits to a grant's terms after creation (trigger).
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.consent.constants import AuditEventType, ConsentAction, GrantorType, RevocationStatus
from app.db import Base
from app.types import BigIntPK, JSONDocument, PurposeScopeSet, UTCDateTime


def enum_column(enum_cls: type, name: str) -> Enum:
    # VARCHAR + CHECK rather than native PostgreSQL enums: values can evolve
    # with an ordinary constraint swap, and SQLite behaves the same.
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=40,
        validate_strings=True,
        values_callable=lambda members: [m.value for m in members],
    )


class DeceasedProfile(Base):
    __tablename__ = "deceased_profile"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    full_name: Mapped[str] = mapped_column(String(300))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    # NULL only while a SELF_PRE_NEED profile's subject is alive.
    date_of_death: Mapped[date | None] = mapped_column(Date)
    # ISO 3166-2 subdivision, e.g. US-CA; governs postmortem publicity rights.
    jurisdiction_state: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint(
            "date_of_birth IS NULL OR date_of_death IS NULL OR date_of_birth <= date_of_death",
            name="life_dates_ordered",
        ),
        CheckConstraint("length(full_name) > 0", name="full_name_present"),
    )


class ConsentGrant(Base):
    __tablename__ = "consent_grant"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    deceased_profile_id: Mapped[UUID] = mapped_column(ForeignKey("deceased_profile.id", ondelete="RESTRICT"), index=True)
    grantor_type: Mapped[GrantorType] = mapped_column(enum_column(GrantorType, "grantor_type"))
    # Authenticated account that created the grant: the OWNER relationship.
    grantor_user_id: Mapped[str] = mapped_column(String(255), index=True)
    grantor_legal_name: Mapped[str] = mapped_column(String(300))
    grantor_contact_email: Mapped[str] = mapped_column(String(320))
    legal_authority_document_url: Mapped[str] = mapped_column(String(2048))
    authority_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    authority_verified_by: Mapped[str | None] = mapped_column(String(255))
    purpose_scope: Mapped[frozenset] = mapped_column(PurposeScopeSet())
    # NULL means every named beneficiary must acknowledge.
    required_acknowledgments: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revocation_reason: Mapped[str | None] = mapped_column(Text)

    beneficiaries: Mapped[list["GrantBeneficiary"]] = relationship(
        order_by="GrantBeneficiary.beneficiary_email", lazy="selectin"
    )
    acknowledgments: Mapped[list["BeneficiaryAcknowledgment"]] = relationship(
        order_by="BeneficiaryAcknowledgment.acknowledged_at", lazy="selectin", viewonly=True
    )

    __table_args__ = (
        CheckConstraint("revoked_at IS NULL OR NOT is_active", name="revoked_implies_inactive"),
        CheckConstraint(
            "(authority_verified_at IS NULL) = (authority_verified_by IS NULL)", name="verification_pair"
        ),
        CheckConstraint("revocation_reason IS NULL OR revoked_at IS NOT NULL", name="reason_requires_revocation"),
        CheckConstraint(
            "required_acknowledgments IS NULL OR required_acknowledgments >= 0", name="required_acks_nonnegative"
        ),
        CheckConstraint(
            "cardinality(purpose_scope) > 0 AND purpose_scope <@ "
            "ARRAY['MEMORIAL_VIEW','VOICE_SYNTHESIS','SCRIPT_GENERATION']::varchar[]",
            name="purpose_scope_valid",
        ).ddl_if(dialect="postgresql"),
    )


class GrantBeneficiary(Base):
    """Beneficiaries named on a grant: the acknowledgment quorum's electorate."""

    __tablename__ = "grant_beneficiary"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    consent_grant_id: Mapped[UUID] = mapped_column(ForeignKey("consent_grant.id", ondelete="RESTRICT"))
    beneficiary_name: Mapped[str] = mapped_column(String(300))
    beneficiary_email: Mapped[str] = mapped_column(String(320))  # stored lower-case
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (UniqueConstraint("consent_grant_id", "beneficiary_email", name="uq_grant_beneficiary_grant_email"),)


class BeneficiaryAcknowledgment(Base):
    __tablename__ = "beneficiary_acknowledgment"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    consent_grant_id: Mapped[UUID] = mapped_column(ForeignKey("consent_grant.id", ondelete="RESTRICT"))
    beneficiary_name: Mapped[str] = mapped_column(String(300))
    beneficiary_email: Mapped[str] = mapped_column(String(320))
    acknowledgment_signature_hash: Mapped[str] = mapped_column(String(64))
    acknowledged_at: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (
        UniqueConstraint("consent_grant_id", "beneficiary_email", name="uq_beneficiary_ack_grant_email"),
        ForeignKeyConstraint(
            ["consent_grant_id", "beneficiary_email"],
            ["grant_beneficiary.consent_grant_id", "grant_beneficiary.beneficiary_email"],
            name="fk_beneficiary_acknowledgment_named_beneficiary",
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(acknowledgment_signature_hash) = 64", name="signature_hash_sha256"),
    )


class AuditEvent(Base):
    """Append-only, hash-chained per profile. Never updated, never deleted.

    deceased_profile_id and consent_grant_id are deliberately not foreign
    keys: the ledger must be able to record attempts against unknown ids and
    must never be coupled to the lifecycle of mutable tables.
    """

    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    event_type: Mapped[AuditEventType] = mapped_column(enum_column(AuditEventType, "audit_event_type"))
    actor_id: Mapped[str] = mapped_column(String(255))
    consent_grant_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    deceased_profile_id: Mapped[UUID] = mapped_column(Uuid)
    payload: Mapped[dict] = mapped_column(JSONDocument)
    previous_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (
        UniqueConstraint("deceased_profile_id", "previous_hash", name="uq_audit_event_chain_link"),
        Index("ix_audit_event_profile_chain", "deceased_profile_id", "id"),
        CheckConstraint("length(previous_hash) = 64 AND length(event_hash) = 64", name="hashes_sha256"),
    )


class RevocationRequest(Base):
    """Also the transactional outbox for the cascade: a row that isn't
    COMPLETE is work the sweeper will (re)dispatch."""

    __tablename__ = "revocation_request"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    consent_grant_id: Mapped[UUID] = mapped_column(ForeignKey("consent_grant.id", ondelete="RESTRICT"), unique=True)
    requested_by: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[RevocationStatus] = mapped_column(enum_column(RevocationStatus, "revocation_status"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    cascade_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint(
            "(status = 'COMPLETE') = (cascade_completed_at IS NOT NULL)", name="completion_timestamp_matches_status"
        ),
        Index("ix_revocation_request_open", "status", "updated_at"),
    )


class AuthorizationToken(Base):
    """Every token the kernel issued: the revocation list downstream
    validators check. Downstream roles may read only this table."""

    __tablename__ = "authorization_token"

    jti: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    consent_grant_id: Mapped[UUID | None] = mapped_column(ForeignKey("consent_grant.id", ondelete="RESTRICT"), index=True)
    deceased_profile_id: Mapped[UUID] = mapped_column(ForeignKey("deceased_profile.id", ondelete="RESTRICT"))
    action: Mapped[ConsentAction] = mapped_column(enum_column(ConsentAction, "consent_action"))
    purpose: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(255))
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (CheckConstraint("expires_at > issued_at", name="expiry_after_issue"),)
