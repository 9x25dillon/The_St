"""Engine and session plumbing.

Units of work use `with session_factory.begin() as session:` so a block
either commits every write together with its audit event or none of them.
"""
from __future__ import annotations

from sqlalchemy import Engine, MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


SessionFactory = sessionmaker[Session]

# SQLite stand-ins for the PostgreSQL triggers the migration installs, so the
# fast unit suite also exercises append-only behaviour.
SQLITE_GUARDS = (
    """CREATE TRIGGER IF NOT EXISTS audit_event_no_update BEFORE UPDATE ON audit_event
       BEGIN SELECT RAISE(ABORT, 'audit_event is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS audit_event_no_delete BEFORE DELETE ON audit_event
       BEGIN SELECT RAISE(ABORT, 'audit_event is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS beneficiary_acknowledgment_no_update BEFORE UPDATE ON beneficiary_acknowledgment
       BEGIN SELECT RAISE(ABORT, 'beneficiary_acknowledgment is append-only'); END""",
)


def make_engine(url: str, *, echo: bool = False) -> Engine:
    if url.startswith("sqlite"):
        in_memory = url in ("sqlite://", "sqlite+pysqlite://") or ":memory:" in url
        engine = create_engine(
            url,
            echo=echo,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool if in_memory else None,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - driver callback
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(url, echo=echo, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def create_sqlite_schema(engine: Engine) -> None:
    """Development/test schema for SQLite. PostgreSQL uses Alembic only."""
    if engine.dialect.name != "sqlite":
        raise RuntimeError("create_sqlite_schema is for SQLite; run `alembic upgrade head` for PostgreSQL")
    import app.models  # noqa: F401  (register every table)

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for statement in SQLITE_GUARDS:
            connection.exec_driver_sql(statement)


def is_postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"
