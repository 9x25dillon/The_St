"""What a beneficiary acknowledges, and how the acknowledgment is bound.

The statement is a canonical document naming the exact grant and terms.
The stored acknowledgment_signature_hash is

    sha256(canonical_json({"statement": <statement>, "signature": <signature>}))

so it commits to both what was acknowledged and the signature given. The
raw signature is never stored; the beneficiary keeps the statement and can
later prove what they signed by recomputing the hash.
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.consent.audit import canonical_json
from app.consent.constants import ACK_STATEMENT_VERSION
from app.consent.models import ConsentGrant

TERMS = (
    "Consent covers memorialization only.",
    "It never permits financial, property, legal-representation or commercial use.",
    "Any synthesized voice must be presented as synthetic; it does not speak as the deceased.",
    "Consent can be revoked at any time by any authorized party; revocation deletes derived voice data.",
)


def statement_for(grant: ConsentGrant, beneficiary_email: str) -> dict[str, Any]:
    return {
        "version": ACK_STATEMENT_VERSION,
        "consent_grant_id": str(grant.id),
        "deceased_profile_id": str(grant.deceased_profile_id),
        "grantor_type": str(grant.grantor_type.value if hasattr(grant.grantor_type, "value") else grant.grantor_type),
        "grantor_legal_name": grant.grantor_legal_name,
        "purpose_scope": sorted(p.value for p in grant.purpose_scope),
        "beneficiary_email": beneficiary_email,
        "terms": list(TERMS),
    }


def statement_digest(statement: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(statement).encode()).hexdigest()


def signature_hash(statement: dict[str, Any], signature: str) -> str:
    return hashlib.sha256(canonical_json({"statement": statement, "signature": signature}).encode()).hexdigest()
