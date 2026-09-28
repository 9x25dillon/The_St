"""Closed vocabulary of the consent layer.

Every set here is closed on purpose: anything not listed is denied. The
purpose denylist is hardcoded (not configuration) so no deploy, feature flag
or database row can re-enable a banned purpose.
"""
from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping
from uuid import UUID


class GrantorType(StrEnum):
    EXECUTOR = "EXECUTOR"
    ADMINISTRATOR = "ADMINISTRATOR"
    BENEFICIARY = "BENEFICIARY"
    SELF_PRE_NEED = "SELF_PRE_NEED"


# Grantor types that only exist once there is an estate, i.e. after death.
ESTATE_GRANTOR_TYPES: Final[frozenset[GrantorType]] = frozenset(
    {GrantorType.EXECUTOR, GrantorType.ADMINISTRATOR, GrantorType.BENEFICIARY}
)


class PurposeScope(StrEnum):
    """What a grant may be used for. Memorialization only."""

    MEMORIAL_VIEW = "MEMORIAL_VIEW"
    VOICE_SYNTHESIS = "VOICE_SYNTHESIS"
    SCRIPT_GENERATION = "SCRIPT_GENERATION"


class DeclaredPurpose(StrEnum):
    """Purpose a caller declares on each kernel request.

    The first three mirror PurposeScope (use purposes, checked against the
    grant). The last two are data-subject rights purposes that never need a
    grant's scope: access/portability and erasure.
    """

    MEMORIAL_VIEW = "MEMORIAL_VIEW"
    VOICE_SYNTHESIS = "VOICE_SYNTHESIS"
    SCRIPT_GENERATION = "SCRIPT_GENERATION"
    DATA_PORTABILITY = "DATA_PORTABILITY"
    ERASURE = "ERASURE"


class BannedPurpose(StrEnum):
    """Purposes that are never permitted, under any grant, for anyone.

    Death terminates agency: nothing on this platform may act as the deceased
    in financial, property, legal or commercial matters.
    """

    FINANCIAL = "FINANCIAL"
    PROPERTY_CLAIM = "PROPERTY_CLAIM"
    LEGAL_REPRESENTATION = "LEGAL_REPRESENTATION"
    COMMERCIAL_IMPERSONATION = "COMMERCIAL_IMPERSONATION"


BANNED_PURPOSES: Final[frozenset[str]] = frozenset(p.value for p in BannedPurpose)


class ConsentAction(StrEnum):
    VIEW_MEMORIAL = "VIEW_MEMORIAL"
    INITIATE_VOICE_SYNTHESIS = "INITIATE_VOICE_SYNTHESIS"
    GENERATE_SCRIPT = "GENERATE_SCRIPT"
    DELIVER_MESSAGE = "DELIVER_MESSAGE"
    EXPORT_DATA = "EXPORT_DATA"
    DELETE_VOICE_MODEL = "DELETE_VOICE_MODEL"


# The one purpose each action may be requested under. A request whose
# declared purpose differs is denied, so a narrow purpose can't be laundered
# into a broader action.
ACTION_PURPOSE: Final[Mapping[ConsentAction, DeclaredPurpose]] = MappingProxyType(
    {
        ConsentAction.VIEW_MEMORIAL: DeclaredPurpose.MEMORIAL_VIEW,
        ConsentAction.INITIATE_VOICE_SYNTHESIS: DeclaredPurpose.VOICE_SYNTHESIS,
        ConsentAction.GENERATE_SCRIPT: DeclaredPurpose.SCRIPT_GENERATION,
        ConsentAction.DELIVER_MESSAGE: DeclaredPurpose.VOICE_SYNTHESIS,
        ConsentAction.EXPORT_DATA: DeclaredPurpose.DATA_PORTABILITY,
        ConsentAction.DELETE_VOICE_MODEL: DeclaredPurpose.ERASURE,
    }
)

# Use actions consume a grant's purpose scope; rights actions (access,
# erasure) must keep working after revocation, so they don't.
USE_ACTIONS: Final[frozenset[ConsentAction]] = frozenset(
    {
        ConsentAction.VIEW_MEMORIAL,
        ConsentAction.INITIATE_VOICE_SYNTHESIS,
        ConsentAction.GENERATE_SCRIPT,
        ConsentAction.DELIVER_MESSAGE,
    }
)
RIGHTS_ACTIONS: Final[frozenset[ConsentAction]] = frozenset(ConsentAction) - USE_ACTIONS
SYNTHESIS_ACTIONS: Final[frozenset[ConsentAction]] = USE_ACTIONS - {ConsentAction.VIEW_MEMORIAL}


class AuditEventType(StrEnum):
    # Specified in the Consent Kernel v1 brief.
    CONSENT_CREATED = "CONSENT_CREATED"
    CONSENT_REVOKED = "CONSENT_REVOKED"
    AUTHORITY_VERIFIED = "AUTHORITY_VERIFIED"
    SYNTHESIS_REQUESTED = "SYNTHESIS_REQUESTED"
    SYNTHESIS_BLOCKED = "SYNTHESIS_BLOCKED"
    MODEL_DELETED = "MODEL_DELETED"
    DATA_EXPORTED = "DATA_EXPORTED"
    PURPOSE_VIOLATION_ATTEMPT = "PURPOSE_VIOLATION_ATTEMPT"
    # Extensions: every write must be audited, and these writes had no type.
    PROFILE_CREATED = "PROFILE_CREATED"
    DEATH_RECORDED = "DEATH_RECORDED"
    BENEFICIARY_ACKNOWLEDGED = "BENEFICIARY_ACKNOWLEDGED"
    SUCCESSOR_DESIGNATED = "SUCCESSOR_DESIGNATED"
    AUTHORIZATION_GRANTED = "AUTHORIZATION_GRANTED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    DELIVERY_CANCELLED = "DELIVERY_CANCELLED"
    REVOCATION_CASCADE_COMPLETED = "REVOCATION_CASCADE_COMPLETED"


class RevocationStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETE = "COMPLETE"


class Role(StrEnum):
    """Relationship of an actor to a grant or profile, resolved per request."""

    OWNER = "OWNER"
    BENEFICIARY = "BENEFICIARY"
    SUCCESSOR = "SUCCESSOR"
    ADMIN = "ADMIN"
    PUBLIC = "PUBLIC"


class DenialReason(StrEnum):
    PURPOSE_VIOLATION = "PURPOSE_VIOLATION"
    UNKNOWN_PURPOSE = "UNKNOWN_PURPOSE"
    PURPOSE_ACTION_MISMATCH = "PURPOSE_ACTION_MISMATCH"
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    NON_HUMAN_ACTOR = "NON_HUMAN_ACTOR"
    DECEASED_CANNOT_ACT = "DECEASED_CANNOT_ACT"
    SUBJECT_NOT_DECEASED = "SUBJECT_NOT_DECEASED"
    NO_GRANT = "NO_GRANT"
    ROLE_NOT_PERMITTED = "ROLE_NOT_PERMITTED"
    GRANT_REVOKED = "GRANT_REVOKED"
    GRANT_INACTIVE = "GRANT_INACTIVE"
    AUTHORITY_NOT_VERIFIED = "AUTHORITY_NOT_VERIFIED"
    PURPOSE_NOT_IN_SCOPE = "PURPOSE_NOT_IN_SCOPE"
    ACKNOWLEDGMENT_QUORUM_NOT_MET = "ACKNOWLEDGMENT_QUORUM_NOT_MET"


# Reasons a caller without a relationship to the grant may see. Everything
# else collapses to NOT_AUTHORIZED so a stranger can't learn, say, that a
# family revoked consent.
PUBLIC_DENIAL_REASONS: Final[frozenset[DenialReason]] = frozenset(
    {
        DenialReason.PURPOSE_VIOLATION,
        DenialReason.UNKNOWN_PURPOSE,
        DenialReason.PURPOSE_ACTION_MISMATCH,
        DenialReason.NON_HUMAN_ACTOR,
    }
)
GENERIC_PUBLIC_DENIAL = "NOT_AUTHORIZED"

GENESIS_HASH: Final[str] = "0" * 64
# Audit chain for events that can't be attributed to a real profile (for
# example a banned-purpose request naming a malformed profile id).
UNATTRIBUTED_PROFILE_ID: Final[UUID] = UUID(int=0)
ACK_STATEMENT_VERSION: Final[str] = "remembrance-ack/v1"

_SEPARATORS = re.compile(r"[\s\-./:]+")


def normalize_purpose(code: str) -> str:
    """Canonical form of a declared purpose code.

    NFKC folds compatibility characters (full-width letters), format
    characters such as zero-width spaces are dropped, case is folded and
    separators collapse to underscores, so "financial", "Financial",
    "FINAN\\u200bCIAL" and "Ｆｉｎａｎｃｉａｌ" all normalize to FINANCIAL.
    """
    text = unicodedata.normalize("NFKC", code)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = _SEPARATORS.sub("_", text.strip().upper())
    return text.strip("_")


_BANNED_TOKENS: Final[tuple[tuple[str, ...], ...]] = tuple(tuple(p.split("_")) for p in sorted(BANNED_PURPOSES))


def is_banned_purpose(code: str) -> bool:
    """True when the code is, or contains as whole tokens, a banned purpose.

    Token containment makes variants such as FINANCIAL_TRANSFER or
    COMMERCIAL_IMPERSONATION_V2 count as violation attempts rather than
    ordinary unknown purposes. Both are denied either way; this decides
    whether the attempt is audited as a purpose violation.
    """
    normalized = normalize_purpose(code)
    if normalized in BANNED_PURPOSES:
        return True
    tokens = tuple(normalized.split("_"))
    for banned in _BANNED_TOKENS:
        width = len(banned)
        if any(tokens[i : i + width] == banned for i in range(len(tokens) - width + 1)):
            return True
    return False


def parse_declared_purpose(code: str) -> DeclaredPurpose | None:
    try:
        return DeclaredPurpose(normalize_purpose(code))
    except ValueError:
        return None
