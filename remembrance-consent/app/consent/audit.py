"""Append-only, hash-chained audit ledger.

Each deceased profile owns one chain. An event's hash commits to every
column that gives it meaning, and to its predecessor:

    event_hash = sha256(canonical_json({
        "v": 1, "event_type", "actor_id", "deceased_profile_id",
        "consent_grant_id", "payload", "previous_hash", "created_at"}))

The brief asks for sha256(payload + previous_hash + timestamp); this is a
superset, so editing event_type, actor_id or the ids is detected too.
Encoding one JSON object (sorted keys, no whitespace) instead of a string
concatenation leaves no delimiter ambiguity.

Chain invariants, all checked by verify_chain():
  I1  the first event of a chain has previous_hash = GENESIS_HASH
  I2  every later event's previous_hash equals its predecessor's event_hash
  I3  every event_hash recomputes from the row
  I4  created_at never decreases along a chain
  I5  (optional) the chain still contains previously anchored heads, which
      catches truncation, the one edit a hash chain can't see on its own

Payloads carry identifiers, enums, counts and digests, never raw personal
data: rows in an append-only ledger can't be erased, so PII must not enter.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.consent.constants import GENESIS_HASH, AuditEventType
from app.consent.models import AuditEvent
from app.db import is_postgres

HASH_FORMAT_VERSION = 1
MAX_PAYLOAD_BYTES = 16_384
MAX_PAYLOAD_DEPTH = 8
MAX_SAFE_INTEGER = 2**53 - 1
_LOCK_NAMESPACE = b"remembrance-audit-chain:"


class AuditPayloadError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def format_timestamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _check_value(value: Any, depth: int) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise AuditPayloadError(f"payload nested deeper than {MAX_PAYLOAD_DEPTH}")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise AuditPayloadError("payload integers must fit in 53 bits to survive JSON round trips")
        return
    if isinstance(value, float):
        # JSONB may re-render floats; a hash over them could stop recomputing.
        raise AuditPayloadError("payload floats are not allowed; use integers or strings")
    if isinstance(value, str):
        if "\x00" in value:
            raise AuditPayloadError("payload strings must not contain NUL")
        return
    if isinstance(value, list):
        for item in value:
            _check_value(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or "\x00" in key:
                raise AuditPayloadError("payload keys must be strings without NUL")
            _check_value(item, depth + 1)
        return
    raise AuditPayloadError(f"unsupported payload type {type(value).__name__}")


def validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AuditPayloadError("payload must be an object")
    document = dict(payload)
    _check_value(document, 0)
    if len(canonical_json(document).encode()) > MAX_PAYLOAD_BYTES:
        raise AuditPayloadError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return document


def compute_event_hash(
    *,
    event_type: str,
    actor_id: str,
    deceased_profile_id: UUID,
    consent_grant_id: UUID | None,
    payload: Mapping[str, Any],
    previous_hash: str,
    created_at: datetime,
) -> str:
    material = canonical_json(
        {
            "v": HASH_FORMAT_VERSION,
            "event_type": str(event_type),
            "actor_id": actor_id,
            "deceased_profile_id": str(deceased_profile_id),
            "consent_grant_id": str(consent_grant_id) if consent_grant_id else None,
            "payload": payload,
            "previous_hash": previous_hash,
            "created_at": format_timestamp(created_at),
        }
    )
    return hashlib.sha256(material.encode()).hexdigest()


def chain_lock_key(deceased_profile_id: UUID) -> int:
    """Signed 64-bit advisory-lock key, namespaced so it can't collide with
    other advisory locks in the same database."""
    digest = hashlib.sha256(_LOCK_NAMESPACE + deceased_profile_id.bytes).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def append_event(
    session: Session,
    *,
    clock: Clock,
    event_type: AuditEventType,
    actor_id: str,
    deceased_profile_id: UUID,
    payload: Mapping[str, Any],
    consent_grant_id: UUID | None = None,
) -> AuditEvent:
    """Append one event to the profile's chain inside the caller's transaction.

    Call it as the last write of a unit of work: on PostgreSQL it takes a
    transaction-scoped advisory lock on the chain, which serializes appenders
    so each reads the true head (READ COMMITTED sees the previous commit).
    The unique (profile, previous_hash) constraint turns any fork that slips
    through into an IntegrityError instead of a silent branch.
    """
    document = validate_payload(payload)
    if is_postgres(session):
        session.execute(select(func.pg_advisory_xact_lock(chain_lock_key(deceased_profile_id))))
    head = session.execute(
        select(AuditEvent.event_hash, AuditEvent.created_at)
        .where(AuditEvent.deceased_profile_id == deceased_profile_id)
        .order_by(AuditEvent.id.desc())
        .limit(1)
    ).first()
    previous_hash = head.event_hash if head else GENESIS_HASH
    created_at = clock.now()
    if head is not None and created_at < head.created_at:
        created_at = head.created_at  # keep I4 under cross-host clock skew
    event = AuditEvent(
        event_type=event_type,
        actor_id=actor_id,
        consent_grant_id=consent_grant_id,
        deceased_profile_id=deceased_profile_id,
        payload=document,
        previous_hash=previous_hash,
        event_hash=compute_event_hash(
            event_type=event_type.value,
            actor_id=actor_id,
            deceased_profile_id=deceased_profile_id,
            consent_grant_id=consent_grant_id,
            payload=document,
            previous_hash=previous_hash,
            created_at=created_at,
        ),
        created_at=created_at,
    )
    session.add(event)
    session.flush()
    return event


@dataclass(frozen=True, slots=True)
class EventRecord:
    """Storage-independent view of an event (DB row or exported JSONL line)."""

    id: int
    event_type: str
    actor_id: str
    consent_grant_id: UUID | None
    deceased_profile_id: UUID
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: AuditEvent) -> "EventRecord":
        return cls(
            id=row.id,
            event_type=str(row.event_type.value if isinstance(row.event_type, AuditEventType) else row.event_type),
            actor_id=row.actor_id,
            consent_grant_id=row.consent_grant_id,
            deceased_profile_id=row.deceased_profile_id,
            payload=row.payload,
            previous_hash=row.previous_hash,
            event_hash=row.event_hash,
            created_at=row.created_at,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "actor_id": self.actor_id,
            "consent_grant_id": str(self.consent_grant_id) if self.consent_grant_id else None,
            "deceased_profile_id": str(self.deceased_profile_id),
            "payload": self.payload,
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
            "created_at": format_timestamp(self.created_at),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "EventRecord":
        return cls(
            id=int(data["id"]),
            event_type=str(data["event_type"]),
            actor_id=str(data["actor_id"]),
            consent_grant_id=UUID(data["consent_grant_id"]) if data.get("consent_grant_id") else None,
            deceased_profile_id=UUID(data["deceased_profile_id"]),
            payload=dict(data["payload"]),
            previous_hash=str(data["previous_hash"]),
            event_hash=str(data["event_hash"]),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    def recomputed_hash(self) -> str:
        return compute_event_hash(
            event_type=self.event_type,
            actor_id=self.actor_id,
            deceased_profile_id=self.deceased_profile_id,
            consent_grant_id=self.consent_grant_id,
            payload=self.payload,
            previous_hash=self.previous_hash,
            created_at=self.created_at,
        )


class ChainErrorKind(StrEnum):
    BAD_GENESIS = "BAD_GENESIS"
    BROKEN_LINK = "BROKEN_LINK"
    HASH_MISMATCH = "HASH_MISMATCH"
    TIMESTAMP_REGRESSION = "TIMESTAMP_REGRESSION"
    ANCHOR_MISMATCH = "ANCHOR_MISMATCH"
    ANCHOR_MISSING = "ANCHOR_MISSING"


@dataclass(frozen=True, slots=True)
class ChainError:
    deceased_profile_id: UUID
    event_id: int | None
    kind: ChainErrorKind
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "deceased_profile_id": str(self.deceased_profile_id),
            "event_id": self.event_id,
            "kind": self.kind.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Anchor:
    """A previously observed (length, head_hash) of one chain."""

    length: int
    head_hash: str


@dataclass(slots=True)
class ChainSummary:
    deceased_profile_id: UUID
    length: int = 0
    head_hash: str = GENESIS_HASH
    head_event_id: int | None = None
    last_created_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "deceased_profile_id": str(self.deceased_profile_id),
            "length": self.length,
            "head_hash": self.head_hash,
            "head_event_id": self.head_event_id,
        }


@dataclass(slots=True)
class ChainReport:
    chains: dict[UUID, ChainSummary] = field(default_factory=dict)
    errors: list[ChainError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def event_count(self) -> int:
        return sum(c.length for c in self.chains.values())

    def anchors(self) -> dict[str, dict[str, Any]]:
        return {str(pid): {"length": c.length, "head_hash": c.head_hash} for pid, c in sorted(self.chains.items())}

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "chains": len(self.chains),
            "events": self.event_count,
            "errors": [e.to_json() for e in self.errors],
            "heads": [c.to_json() for _, c in sorted(self.chains.items())],
        }


def verify_chain(events: Iterable[EventRecord], anchors: Mapping[UUID, Anchor] | None = None) -> ChainReport:
    """Single pass, O(n) time, O(number of chains) memory.

    `events` must be in id order within each chain (any interleaving of
    chains is fine). Verification continues from each event's *stored* hash,
    so one tampered row yields one error instead of failing every later row.
    """
    anchors = dict(anchors or {})
    report = ChainReport()
    for event in events:
        chain = report.chains.get(event.deceased_profile_id)
        if chain is None:
            chain = report.chains[event.deceased_profile_id] = ChainSummary(event.deceased_profile_id)
        expected_previous = chain.head_hash
        if event.previous_hash != expected_previous:
            kind = ChainErrorKind.BAD_GENESIS if chain.length == 0 else ChainErrorKind.BROKEN_LINK
            report.errors.append(
                ChainError(
                    event.deceased_profile_id,
                    event.id,
                    kind,
                    f"previous_hash {event.previous_hash[:12]}… expected {expected_previous[:12]}…",
                )
            )
        if event.recomputed_hash() != event.event_hash:
            report.errors.append(
                ChainError(event.deceased_profile_id, event.id, ChainErrorKind.HASH_MISMATCH, "row contents do not match event_hash")
            )
        if chain.last_created_at is not None and event.created_at < chain.last_created_at:
            report.errors.append(
                ChainError(event.deceased_profile_id, event.id, ChainErrorKind.TIMESTAMP_REGRESSION, "created_at went backwards")
            )
        chain.length += 1
        chain.head_hash = event.event_hash
        chain.head_event_id = event.id
        chain.last_created_at = event.created_at
        anchor = anchors.get(event.deceased_profile_id)
        if anchor is not None and chain.length == anchor.length and event.event_hash != anchor.head_hash:
            report.errors.append(
                ChainError(
                    event.deceased_profile_id,
                    event.id,
                    ChainErrorKind.ANCHOR_MISMATCH,
                    f"event #{anchor.length} of this chain differs from the anchored head",
                )
            )
    for profile_id, anchor in anchors.items():
        length = report.chains[profile_id].length if profile_id in report.chains else 0
        if length < anchor.length:
            report.errors.append(
                ChainError(profile_id, None, ChainErrorKind.ANCHOR_MISSING, f"chain has {length} events; anchor saw {anchor.length}")
            )
    return report


def iter_events(session: Session, deceased_profile_id: UUID | None = None, batch_size: int = 1000) -> Iterator[EventRecord]:
    statement = select(AuditEvent).order_by(AuditEvent.id).execution_options(yield_per=batch_size)
    if deceased_profile_id is not None:
        statement = statement.where(AuditEvent.deceased_profile_id == deceased_profile_id)
    for row in session.scalars(statement):
        yield EventRecord.from_row(row)


def parse_anchors(document: Mapping[str, Any]) -> dict[UUID, Anchor]:
    return {
        UUID(profile_id): Anchor(length=int(value["length"]), head_hash=str(value["head_hash"]))
        for profile_id, value in document.items()
    }
