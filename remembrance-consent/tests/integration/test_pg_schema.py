"""Migrations, append-only ledger, grant state machine and role isolation on
real PostgreSQL."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import psycopg.errors as pgerr
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

import app.models  # noqa: F401
from app.clock import ManualClock
from app.consent.audit import append_event, iter_events, verify_chain
from app.consent.constants import AuditEventType
from app.consent.tokens import SqlRevocationChecker, TokenStatus
from app.db import Base, make_engine, make_session_factory
from scripts import verify_audit_chain
from tests.factories import seed_grant, seed_profile, seed_token
from tests.integration.conftest import migrate

T0 = datetime(2026, 9, 28, 12, tzinfo=UTC)


@pytest.fixture
def clock():
    return ManualClock(T0)


@pytest.fixture
def app_sf(pg):
    engine = make_engine(pg.app_url)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def admin_engine(pg):
    engine = create_engine(pg.admin_url)
    yield engine
    engine.dispose()


def rejected(engine, statement, params=None) -> DBAPIError:
    with pytest.raises(DBAPIError) as caught:
        with engine.begin() as connection:
            connection.execute(text(statement), params or {})
    return caught.value


def seeded_ledger(app_sf, clock, n=4):
    profile_id = seed_profile(app_sf, clock)
    for i in range(n):
        with app_sf.begin() as s:
            append_event(s, clock=clock, event_type=AuditEventType.CONSENT_CREATED, actor_id="a", deceased_profile_id=profile_id,
                         payload={"n": i, "text": "café"})
        clock.advance(microseconds=1)
    return profile_id


# --- migrations -------------------------------------------------------------------


def test_migration_matches_models_and_round_trips(empty_pg):
    migrate(empty_pg)
    engine = create_engine(empty_pg)
    try:
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata) == []
        migrate(empty_pg, "base", down=True)
        assert inspect(engine).get_table_names() == ["alembic_version"]
        migrate(empty_pg)
        assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
    finally:
        engine.dispose()


# --- append-only ledger ------------------------------------------------------------


@pytest.mark.parametrize("statement", ["UPDATE audit_event SET actor_id = 'mallory'", "DELETE FROM audit_event", "TRUNCATE audit_event"])
def test_runtime_role_cannot_mutate_the_ledger(pg, app_sf, clock, statement):
    seeded_ledger(app_sf, clock)
    engine = make_engine(pg.app_url)
    error = rejected(engine, statement)
    engine.dispose()
    assert isinstance(error.orig, pgerr.InsufficientPrivilege) and "permission denied" in str(error.orig)


@pytest.mark.parametrize("statement", ["UPDATE audit_event SET actor_id = 'mallory'", "DELETE FROM audit_event", "TRUNCATE audit_event"])
def test_even_the_owner_is_stopped_by_triggers(admin_engine, app_sf, clock, statement):
    seeded_ledger(app_sf, clock)
    error = rejected(admin_engine, statement)
    assert "audit_event is append-only" in str(error.orig)


def test_acknowledgments_are_append_only(pg, app_sf, clock, admin_engine):
    profile_id = seed_profile(app_sf, clock)
    seed_grant(app_sf, clock, profile_id)
    engine = make_engine(pg.app_url)
    assert isinstance(rejected(engine, "UPDATE beneficiary_acknowledgment SET beneficiary_name = 'x'").orig, pgerr.InsufficientPrivilege)
    engine.dispose()
    assert "append-only" in str(rejected(admin_engine, "DELETE FROM beneficiary_acknowledgment").orig)


def test_superuser_bypass_is_caught_by_the_hash_chain(pg, app_sf, clock, admin_engine, capsys):
    profile_id = seeded_ledger(app_sf, clock)
    assert verify_audit_chain.main(["--database-url", pg.app_url]) == 0
    with admin_engine.begin() as connection:
        connection.execute(text("SET LOCAL session_replication_role = replica"))  # disables triggers
        connection.execute(text("UPDATE audit_event SET payload = '{\"n\": 99, \"text\": \"café\"}' WHERE id = 2"))
    with app_sf() as s:
        report = verify_chain(iter_events(s, profile_id))
    assert [e.kind.value for e in report.errors] == ["HASH_MISMATCH"] and report.errors[0].event_id == 2
    assert verify_audit_chain.main(["--database-url", pg.app_url]) == 1
    assert "HASH_MISMATCH" in capsys.readouterr().out


def test_hashes_survive_jsonb_and_timestamptz_round_trips(app_sf, clock):
    profile_id = seeded_ledger(app_sf, clock, n=3)
    with app_sf() as s:
        events = list(iter_events(s, profile_id))
    assert all(e.recomputed_hash() == e.event_hash for e in events)
    assert events[0].created_at.microsecond == 0 and events[1].created_at.microsecond == 1


# --- grant and token state machines --------------------------------------------------


def test_consent_terms_are_immutable(pg, app_sf, clock):
    profile_id = seed_profile(app_sf, clock)
    grant = seed_grant(app_sf, clock, profile_id)
    engine = make_engine(pg.app_url)
    params = {"g": grant}
    assert "immutable" in str(rejected(engine, "UPDATE consent_grant SET purpose_scope = ARRAY['MEMORIAL_VIEW'] WHERE id = :g", params).orig)
    assert "immutable" in str(rejected(engine, "UPDATE consent_grant SET grantor_user_id = 'mallory' WHERE id = :g", params).orig)
    assert "write-once" in str(rejected(engine, "UPDATE consent_grant SET authority_verified_by = 'other' WHERE id = :g", params).orig)
    assert isinstance(rejected(engine, "DELETE FROM consent_grant WHERE id = :g", params).orig, pgerr.InsufficientPrivilege)
    with engine.begin() as c:  # contact details may still be corrected
        c.execute(text("UPDATE consent_grant SET grantor_contact_email = 'new@example.com' WHERE id = :g"), params)
    engine.dispose()


def test_revocation_is_terminal(pg, app_sf, clock, admin_engine):
    profile_id = seed_profile(app_sf, clock)
    grant = seed_grant(app_sf, clock, profile_id, revoked=True)
    engine = make_engine(pg.app_url)
    params = {"g": grant}
    assert "irreversible" in str(rejected(engine, "UPDATE consent_grant SET is_active = true, revoked_at = NULL, revocation_reason = NULL WHERE id = :g", params).orig)
    assert "irreversible" in str(rejected(engine, "UPDATE consent_grant SET revocation_reason = 'edited' WHERE id = :g", params).orig)
    engine.dispose()
    assert "never deleted" in str(rejected(admin_engine, "DELETE FROM consent_grant WHERE id = :g", params).orig)


def test_check_constraints(pg, app_sf, clock):
    profile_id = seed_profile(app_sf, clock)
    engine = make_engine(pg.app_url)
    insert = (
        "INSERT INTO consent_grant (id, deceased_profile_id, grantor_type, grantor_user_id, grantor_legal_name, grantor_contact_email, "
        "legal_authority_document_url, purpose_scope, is_active, granted_at, revoked_at) VALUES "
        "(gen_random_uuid(), :p, 'EXECUTOR', 'u', 'n', 'e', 'https://d', CAST(:scope AS varchar[]), :active, now(), :revoked)"
    )
    for scope, active, revoked in ([["FINANCIAL"], True, None], [[], True, None], [["MEMORIAL_VIEW"], True, T0]):
        error = rejected(engine, insert, {"p": profile_id, "scope": scope, "active": active, "revoked": revoked})
        assert isinstance(error.orig, pgerr.CheckViolation)
    error = rejected(engine, "UPDATE deceased_profile SET date_of_birth = '2030-01-01' WHERE id = :p", {"p": profile_id})
    assert isinstance(error.orig, pgerr.CheckViolation)
    engine.dispose()


def test_token_revocation_is_write_once(pg, app_sf, clock, admin_engine):
    profile_id = seed_profile(app_sf, clock)
    jti = seed_token(app_sf, clock, profile_id, seed_grant(app_sf, clock, profile_id))
    engine = make_engine(pg.app_url)
    params = {"j": jti}
    assert "immutable" in str(rejected(engine, "UPDATE authorization_token SET action = 'EXPORT_DATA' WHERE jti = :j", params).orig)
    with engine.begin() as c:
        c.execute(text("UPDATE authorization_token SET revoked_at = now() WHERE jti = :j"), params)
    assert "write-once" in str(rejected(engine, "UPDATE authorization_token SET revoked_at = NULL WHERE jti = :j", params).orig)
    engine.dispose()
    assert "never deleted" in str(rejected(admin_engine, "DELETE FROM authorization_token WHERE jti = :j", params).orig)


# --- role isolation -------------------------------------------------------------------


def test_downstream_role_sees_only_the_revocation_list(pg, app_sf, clock):
    profile_id = seed_profile(app_sf, clock)
    jti = seed_token(app_sf, clock, profile_id, seed_grant(app_sf, clock, profile_id))
    reader = make_engine(pg.reader_url)
    try:
        assert SqlRevocationChecker(make_session_factory(reader)).status(jti) is TokenStatus.ACTIVE
        assert SqlRevocationChecker(make_session_factory(reader)).status(uuid4()) is TokenStatus.UNKNOWN
        for statement in ("SELECT purpose FROM authorization_token", "SELECT * FROM consent_grant", "SELECT * FROM audit_event",
                          "SELECT * FROM deceased_profile", "UPDATE authorization_token SET revoked_at = now()"):
            assert isinstance(rejected(reader, statement).orig, pgerr.InsufficientPrivilege), statement
    finally:
        reader.dispose()
