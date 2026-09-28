"""Request/response contracts. Requests forbid unknown fields so a client
can't smuggle attributes (e.g. `is_active`, `authority_verified_at`) into
a write."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.consent.constants import ConsentAction, GrantorType, PurposeScope

HEX_SHA256 = r"^[0-9a-f]{64}$"
# ISO 3166-2 subdivision code, e.g. US-CA, US-TX, GB-ENG.
JURISDICTION = r"^[A-Z]{2}-[A-Z0-9]{1,3}$"
MAX_BENEFICIARIES = 50


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class Response(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def https_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("must be an https URL")
    if parts.username or parts.password:
        raise ValueError("must not embed credentials")
    if len(value) > 2048:
        raise ValueError("must be at most 2048 characters")
    return value


class ProfileIn(Request):
    full_name: str = Field(min_length=1, max_length=300)
    date_of_birth: date | None = None
    date_of_death: date | None = None
    jurisdiction_state: str = Field(pattern=JURISDICTION)

    @model_validator(mode="after")
    def _dates_ordered(self) -> "ProfileIn":
        if self.date_of_birth and self.date_of_death and self.date_of_birth > self.date_of_death:
            raise ValueError("date_of_birth must not be after date_of_death")
        return self


class ProfileOut(Response):
    id: UUID
    full_name: str
    date_of_birth: date | None
    date_of_death: date | None
    jurisdiction_state: str
    created_at: datetime


class BeneficiaryIn(Request):
    name: str = Field(min_length=1, max_length=300)
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.lower()


class GrantCreate(Request):
    deceased_profile_id: UUID | None = None
    deceased_profile: ProfileIn | None = None
    grantor_type: GrantorType
    grantor_legal_name: str = Field(min_length=1, max_length=300)
    grantor_contact_email: EmailStr
    legal_authority_document_url: str
    purpose_scope: list[PurposeScope] = Field(min_length=1, max_length=len(PurposeScope))
    beneficiaries: list[BeneficiaryIn] = Field(default_factory=list, max_length=MAX_BENEFICIARIES)
    required_acknowledgments: int | None = Field(default=None, ge=0)

    @field_validator("legal_authority_document_url")
    @classmethod
    def _https(cls, value: str) -> str:
        return https_url(value)

    @model_validator(mode="after")
    def _consistent(self) -> "GrantCreate":
        if (self.deceased_profile_id is None) == (self.deceased_profile is None):
            raise ValueError("provide exactly one of deceased_profile_id or deceased_profile")
        if len(set(self.purpose_scope)) != len(self.purpose_scope):
            raise ValueError("purpose_scope must not repeat a purpose")
        emails = [b.email for b in self.beneficiaries]
        if len(set(emails)) != len(emails):
            raise ValueError("beneficiary emails must be unique")
        if self.required_acknowledgments is not None and self.required_acknowledgments > len(emails):
            raise ValueError("required_acknowledgments cannot exceed the number of named beneficiaries")
        return self


class BeneficiaryStatus(BaseModel):
    name: str
    email: str
    acknowledged_at: datetime | None


class Quorum(BaseModel):
    required: int
    acknowledged: int
    satisfied: bool


class GrantOut(BaseModel):
    id: UUID
    deceased_profile_id: UUID
    grantor_type: GrantorType
    grantor_user_id: str
    grantor_legal_name: str
    grantor_contact_email: str
    legal_authority_document_url: str
    authority_verified_at: datetime | None
    authority_verified_by: str | None
    purpose_scope: list[PurposeScope]
    required_acknowledgments: int | None
    is_active: bool
    granted_at: datetime
    revoked_at: datetime | None
    revocation_reason: str | None
    beneficiaries: list[BeneficiaryStatus]
    quorum: Quorum


class VerifyRequest(Request):
    # SHA-256 of the exact authority document (letters testamentary, court
    # order...) the admin reviewed, binding the verification to its bytes.
    document_sha256: str = Field(pattern=HEX_SHA256)


class AcknowledgeRequest(Request):
    # Typed legal name or a detached signature blob; only its hash is stored.
    signature: str = Field(min_length=1, max_length=4096)


class AcknowledgeResponse(BaseModel):
    acknowledgment_id: UUID
    consent_grant_id: UUID
    statement: dict[str, Any]
    acknowledgment_signature_hash: str
    acknowledged_at: datetime
    quorum: Quorum


class RevokeRequest(Request):
    reason: str = Field(min_length=1, max_length=2000)


class RevokeResponse(BaseModel):
    consent_grant_id: UUID
    revoked_at: datetime
    revocation_request_id: UUID
    status: str
    tokens_invalidated: int


class AuthorizeRequest(Request):
    action: ConsentAction
    profile_id: UUID
    purpose_code: str = Field(min_length=1, max_length=64)


class AuthorizeResponse(BaseModel):
    allowed: bool
    action: ConsentAction
    profile_id: UUID
    consent_grant_id: UUID | None = None
    token: str | None = None
    token_type: Literal["consent+jwt"] | None = None
    jti: UUID | None = None
    expires_at: datetime | None = None
    reason: str | None = None


class SuccessorCreate(Request):
    deceased_profile_id: UUID
    successor_user_id: str = Field(min_length=1, max_length=255)
    priority_order: int = Field(ge=1, le=100)


class SuccessorOut(Response):
    id: UUID
    deceased_profile_id: UUID
    successor_user_id: str
    priority_order: int
    designated_at: datetime
    designated_by: str


class DeathRecordRequest(Request):
    date_of_death: date
    death_certificate_sha256: str = Field(pattern=HEX_SHA256)


class ExportResponse(BaseModel):
    export_id: UUID
    url: str
    expires_at: datetime
    manifest_sha256: str
    file_count: int


class AuditEventOut(Response):
    id: int
    event_type: str
    actor_id: str
    consent_grant_id: UUID | None
    deceased_profile_id: UUID
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str
    created_at: datetime


class ChainStatus(BaseModel):
    verified: bool
    length: int
    head_hash: str
    errors: int


class AuditPage(BaseModel):
    events: list[AuditEventOut]
    next_after_id: int | None
    chain: ChainStatus
