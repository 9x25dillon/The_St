"""Real PostgreSQL for integration tests, found in this order:

  1. REMEMBRANCE_TEST_DATABASE_URL  a superuser URL (CI service container)
  2. testcontainers                 postgres:16-alpine, when Docker can pull it
  3. local binaries                 a throwaway cluster from initdb/pg_ctl

If none is available the integration tests are skipped with the reason.
The migration runs once into a template database; each test gets a fresh
copy (CREATE DATABASE ... TEMPLATE), so append-only tables need no cleanup.
"""
from __future__ import annotations

import glob
import os
import pwd
import shutil
import socket
import subprocess
import tempfile
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

ROOT = Path(__file__).resolve().parents[2]
APP_LOGIN = "remembrance_it_app"
READER_LOGIN = "remembrance_it_reader"
PASSWORD = "integration-test-password"


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/integration/" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(pytest.mark.integration)


@dataclass
class Server:
    admin_url: str
    stop: Callable[[], None] = lambda: None
    source: str = "environment"


def _psycopg(url: str) -> str:
    parsed = make_url(url)
    return parsed.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _from_environment() -> Server | None:
    url = os.environ.get("REMEMBRANCE_TEST_DATABASE_URL")
    return Server(_psycopg(url)) if url else None


def _from_testcontainers() -> Server | None:
    try:
        import docker

        try:
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:  # testcontainers < 4.14
            from testcontainers.postgres import PostgresContainer
        docker.from_env().ping()
    except Exception:
        return None
    try:
        container = PostgresContainer("postgres:16-alpine", driver="psycopg")
        container.start()
    except Exception as exc:  # image pull blocked, daemon unhealthy...
        warnings.warn(f"testcontainers unavailable: {exc}", stacklevel=1)
        return None
    return Server(_psycopg(container.get_connection_url()), container.stop, "testcontainers")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _from_local_binaries() -> Server | None:
    initdb = shutil.which("initdb") or next(iter(sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"), reverse=True)), None)
    if initdb is None:
        return None
    pg_ctl = str(Path(initdb).with_name("pg_ctl"))
    prefix: list[str] = []
    datadir = tempfile.mkdtemp(prefix="remembrance-pg-", dir="/var/tmp" if os.path.isdir("/var/tmp") else None)
    if os.geteuid() == 0:  # initdb refuses to run as root
        try:
            account = pwd.getpwnam("postgres")
        except KeyError:
            return None
        os.chown(datadir, account.pw_uid, account.pw_gid)
        prefix = ["runuser", "-u", "postgres", "--"]
    port = _free_port()
    try:
        subprocess.run([*prefix, initdb, "-D", datadir, "-U", "postgres", "--auth=trust", "-E", "UTF8"],
                       check=True, capture_output=True)
        subprocess.run([*prefix, pg_ctl, "-D", datadir, "-w", "-l", f"{datadir}/server.log", "-o",
                        f"-p {port} -k {datadir} -c listen_addresses=127.0.0.1 -c fsync=off -c synchronous_commit=off", "start"],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        warnings.warn(f"local PostgreSQL unavailable: {exc}", stacklevel=1)
        shutil.rmtree(datadir, ignore_errors=True)
        return None

    def stop() -> None:
        subprocess.run([*prefix, pg_ctl, "-D", datadir, "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(datadir, ignore_errors=True)

    return Server(f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres", stop, "local binaries")


@pytest.fixture(scope="session")
def pg_server():
    for factory in (_from_environment, _from_testcontainers, _from_local_binaries):
        server = factory()
        if server is not None:
            break
    else:
        pytest.skip("no PostgreSQL: set REMEMBRANCE_TEST_DATABASE_URL, start Docker, or install PostgreSQL binaries")
    yield server
    server.stop()


def _admin_engine(url: str, database: str = "postgres") -> Engine:
    return create_engine(make_url(url).set(database=database), isolation_level="AUTOCOMMIT")


def migrate(url: str, revision: str = "head", *, down: bool = False) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    (command.downgrade if down else command.upgrade)(config, revision)


@pytest.fixture(scope="session")
def pg_template(pg_server):
    name = f"remembrance_tpl_{uuid4().hex[:10]}"
    admin = _admin_engine(pg_server.admin_url)
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    migrate(make_url(pg_server.admin_url).set(database=name).render_as_string(hide_password=False))
    with admin.connect() as c:
        for login, group in ((APP_LOGIN, "remembrance_app"), (READER_LOGIN, "remembrance_token_reader")):
            exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": login}).scalar()
            if not exists:
                c.execute(text(f"CREATE ROLE {login} LOGIN PASSWORD '{PASSWORD}' IN ROLE {group}"))
    yield name
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@dataclass
class Database:
    name: str
    admin_url: str
    app_url: str
    reader_url: str


@pytest.fixture
def pg(pg_server, pg_template) -> Database:
    name = f"remembrance_it_{uuid4().hex[:10]}"
    admin = _admin_engine(pg_server.admin_url)
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{pg_template}"'))
    base = make_url(pg_server.admin_url).set(database=name)
    yield Database(
        name=name,
        admin_url=base.render_as_string(hide_password=False),
        app_url=base.set(username=APP_LOGIN, password=PASSWORD).render_as_string(hide_password=False),
        reader_url=base.set(username=READER_LOGIN, password=PASSWORD).render_as_string(hide_password=False),
    )
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def empty_pg(pg_server):
    """A database with no migrations applied."""
    name = f"remembrance_raw_{uuid4().hex[:10]}"
    admin = _admin_engine(pg_server.admin_url)
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    yield make_url(pg_server.admin_url).set(database=name).render_as_string(hide_password=False)
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()
