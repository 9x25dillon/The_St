"""Column types that behave identically on PostgreSQL (production,
integration tests) and SQLite (fast unit tests).

Invariants:
- UTCDateTime always returns aware UTC datetimes with microsecond precision,
  so an audit hash computed before INSERT equals one recomputed after SELECT.
- PurposeScopeSet stores a sorted, duplicate-free list and returns a
  frozenset; PostgreSQL stores VARCHAR[] guarded by a CHECK constraint in the
  migration, SQLite stores JSON.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

from app.consent.constants import PurposeScope

# JSONB on PostgreSQL, JSON elsewhere.
JSONDocument = JSON().with_variant(postgresql.JSONB(), "postgresql")

# BIGSERIAL on PostgreSQL; SQLite only autoincrements INTEGER PRIMARY KEY.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not accepted; use an aware UTC datetime")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class PurposeScopeSet(TypeDecorator[frozenset[PurposeScope]]):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.ARRAY(String(32)))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: Any, dialect: Dialect) -> list[str] | None:
        if value is None:
            return None
        return sorted({PurposeScope(v).value for v in value})

    def process_result_value(self, value: Any, dialect: Dialect) -> frozenset[PurposeScope] | None:
        if value is None:
            return None
        return frozenset(PurposeScope(v) for v in value)
