"""Audit chain: construction, invariants I1–I5, tamper detection."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.consent.audit import (
    Anchor,
    AuditPayloadError,
    ChainErrorKind,
    EventRecord,
    append_event,
    chain_lock_key,
    compute_event_hash,
    format_timestamp,
    iter_events,
    parse_anchors,
    validate_payload,
    verify_chain,
)
from app.consent.constants import GENESIS_HASH, AuditEventType

E = AuditEventType


def append(sf, clock, profile_id, n=1, **payload):
    ids = []
    for i in range(n):
        with sf.begin() as s:
            ids.append(
                append_event(
                    s, clock=clock, event_type=E.CONSENT_CREATED, actor_id="actor", deceased_profile_id=profile_id,
                    payload={"n": i, **payload},
                ).id
            )
        clock.advance(seconds=1)
    return ids


def records(sf, profile_id=None) -> list[EventRecord]:
    with sf() as s:
        return list(iter_events(s, profile_id))


def kinds(report):
    return [e.kind for e in report.errors]


def test_chain_links_from_genesis(session_factory, clock):
    profile_id = uuid4()
    append(session_factory, clock, profile_id, 3)
    first, second, third = records(session_factory)
    assert first.previous_hash == GENESIS_HASH
    assert second.previous_hash == first.event_hash and third.previous_hash == second.event_hash
    assert all(r.recomputed_hash() == r.event_hash for r in (first, second, third))
    report = verify_chain(records(session_factory))
    assert report.ok and report.chains[profile_id].head_hash == third.event_hash


def test_chains_are_independent_per_profile(session_factory, clock):
    a, b = uuid4(), uuid4()
    append(session_factory, clock, a, 2)
    append(session_factory, clock, b, 2)
    append(session_factory, clock, a, 1)
    assert [r.previous_hash for r in records(session_factory, b)][0] == GENESIS_HASH
    report = verify_chain(records(session_factory))
    assert report.ok and report.chains[a].length == 3 and report.chains[b].length == 2 and report.event_count == 5
    assert set(report.anchors()) == {str(a), str(b)}
    assert report.to_json()["ok"] is True


def test_hash_commits_to_every_field():
    base = dict(
        event_type="CONSENT_CREATED", actor_id="a", deceased_profile_id=uuid4(), consent_grant_id=None,
        payload={"k": 1}, previous_hash=GENESIS_HASH, created_at=datetime.fromisoformat("2026-09-28T12:00:00+00:00"),
    )
    reference = compute_event_hash(**base)
    variations = [
        {"event_type": "CONSENT_REVOKED"}, {"actor_id": "b"}, {"deceased_profile_id": uuid4()},
        {"consent_grant_id": uuid4()}, {"payload": {"k": 2}}, {"previous_hash": "1" * 64},
        {"created_at": base["created_at"] + timedelta(microseconds=1)},
    ]
    for change in variations:
        assert compute_event_hash(**{**base, **change}) != reference, change
    # Key order and timezone representation do not matter.
    assert compute_event_hash(**{**base, "payload": {"k": 1}}) == reference


def test_clock_regression_is_clamped_to_keep_timestamps_monotonic(session_factory, clock):
    profile_id = uuid4()
    append(session_factory, clock, profile_id, 1)
    clock.advance(hours=-2)
    append(session_factory, clock, profile_id, 1)
    first, second = records(session_factory)
    assert second.created_at == first.created_at
    assert verify_chain(records(session_factory)).ok


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"f": 1.5}, "floats"),
        ({"s": "a\x00b"}, "NUL"),
        ({"a\x00": 1}, "keys"),
        ({1: "x"}, "keys"),
        ({"big": 2**60}, "53 bits"),
        ({"set": {1, 2}}, "unsupported"),
        ({"deep": [[[[[[[[[["x"]]]]]]]]]]}, "deeper"),
        ({"blob": "x" * 20_000}, "exceeds"),
    ],
)
def test_payload_rules(payload, message):
    with pytest.raises(AuditPayloadError, match=message):
        validate_payload(payload)


def test_payload_must_be_a_mapping_and_accepts_json_types():
    with pytest.raises(AuditPayloadError):
        validate_payload(["not", "a", "mapping"])  # type: ignore[arg-type]
    assert validate_payload({"a": [1, "b", None, True, {"c": -3}]}) == {"a": [1, "b", None, True, {"c": -3}]}


def test_timestamps_must_be_aware():
    with pytest.raises(ValueError):
        format_timestamp(datetime(2026, 1, 1))


def test_lock_key_is_a_stable_signed_64_bit_integer():
    profile_id = uuid4()
    key = chain_lock_key(profile_id)
    assert key == chain_lock_key(profile_id) and -(2**63) <= key < 2**63
    assert key != chain_lock_key(uuid4())


def test_jsonl_round_trip_preserves_hashes(session_factory, clock):
    profile_id = uuid4()
    append(session_factory, clock, profile_id, 2, note="café ✓")
    for record in records(session_factory):
        restored = EventRecord.from_json(record.to_json())
        assert restored == record and restored.recomputed_hash() == record.event_hash


# --- tampering ---------------------------------------------------------------

@pytest.fixture
def chain(session_factory, clock):
    profile_id = uuid4()
    append(session_factory, clock, profile_id, 5)
    return profile_id, records(session_factory)


@pytest.mark.parametrize(
    "field,value",
    [("payload", {"n": 99}), ("event_type", "CONSENT_REVOKED"), ("actor_id", "mallory"), ("consent_grant_id", uuid4())],
)
def test_edited_fields_are_detected(chain, field, value):
    _, events = chain
    events[2] = replace(events[2], **{field: value})
    report = verify_chain(events)
    assert kinds(report) == [ChainErrorKind.HASH_MISMATCH] and report.errors[0].event_id == events[2].id


def test_rehashing_an_edited_event_breaks_the_next_link(chain):
    _, events = chain
    forged = replace(events[2], payload={"n": 99})
    events[2] = replace(forged, event_hash=forged.recomputed_hash())
    report = verify_chain(events)
    assert kinds(report) == [ChainErrorKind.BROKEN_LINK] and report.errors[0].event_id == events[3].id


def test_deleted_event_is_detected(chain):
    _, events = chain
    del events[1]
    assert kinds(verify_chain(events)) == [ChainErrorKind.BROKEN_LINK]


def test_reordered_events_are_detected(chain):
    _, events = chain
    events[1], events[2] = events[2], events[1]
    assert ChainErrorKind.BROKEN_LINK in kinds(verify_chain(events))


def test_bad_genesis_is_detected(chain):
    _, events = chain
    assert kinds(verify_chain(events[1:]))[0] is ChainErrorKind.BAD_GENESIS


def test_backdated_event_is_detected(chain):
    _, events = chain
    backdated = replace(events[3], created_at=events[0].created_at - timedelta(days=1))
    events[3] = replace(backdated, event_hash=backdated.recomputed_hash())
    assert ChainErrorKind.TIMESTAMP_REGRESSION in kinds(verify_chain(events))


def test_anchors_detect_truncation_and_rewrites(chain):
    profile_id, events = chain
    anchors = parse_anchors(verify_chain(events).anchors())
    assert verify_chain(events, anchors).ok
    truncated = verify_chain(events[:3], anchors)
    assert kinds(truncated) == [ChainErrorKind.ANCHOR_MISSING] and truncated.errors[0].event_id is None
    rewritten = verify_chain(events[:4] + [replace(events[4], event_hash="e" * 64)], {profile_id: Anchor(5, events[4].event_hash)})
    assert ChainErrorKind.ANCHOR_MISMATCH in kinds(rewritten)
    assert kinds(verify_chain([], {profile_id: Anchor(1, "a" * 64)})) == [ChainErrorKind.ANCHOR_MISSING]
    assert verify_chain(events[:4] + [replace(events[4], event_hash="e" * 64)]).errors[0].to_json()["kind"] == "HASH_MISMATCH"


def test_database_rejects_updates_and_deletes(session_factory, clock, engine):
    append(session_factory, clock, uuid4(), 2)
    for statement in ("UPDATE audit_event SET actor_id = 'mallory'", "DELETE FROM audit_event"):
        with pytest.raises(IntegrityError, match="append-only"):
            with engine.begin() as connection:
                connection.execute(text(statement))
    assert len(records(session_factory)) == 2


def test_forked_chain_is_rejected_by_the_link_constraint(session_factory, clock, engine):
    profile_id = uuid4()
    append(session_factory, clock, profile_id, 1)
    [head] = records(session_factory)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO audit_event (event_type, actor_id, deceased_profile_id, payload, previous_hash, event_hash, created_at) "
                    "VALUES ('CONSENT_CREATED', 'x', :p, '{}', :prev, :h, '2026-09-28 12:00:00')"
                ),
                {"p": profile_id.hex, "prev": head.previous_hash, "h": "b" * 64},
            )
