from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.clock import ManualClock
from app.config import Settings
from app.consent.tokens import KeyRing
from app.db import create_sqlite_schema, make_engine, make_session_factory
from app.main import create_app
from app.services import build_services
from app.storage import LocalFileStorage, UrlSigner
from tests.support import AUTH_SECRET, Api

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
URL_SECRET = b"url-signing-secret-for-tests-0123456789"


class RecordingDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []

    def dispatch(self, consent_grant_id: UUID) -> None:
        self.dispatched.append(consent_grant_id)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def engine():
    engine = make_engine("sqlite://")
    create_sqlite_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def keyring() -> KeyRing:
    return KeyRing.generate("test-kid")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        env="test",
        database_url="sqlite://",
        auth_jwt_key=AUTH_SECRET,
        storage_local_root=str(tmp_path / "objects"),
        url_signing_secret="u" * 40,
        public_base_url="http://testserver",
        authorize_rate_per_minute=600,
        authorize_burst=100,
    )


@pytest.fixture
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "objects", UrlSigner(URL_SECRET), "http://testserver")


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def services(settings, engine, clock, keyring, storage, dispatcher):
    return build_services(settings, engine=engine, clock=clock, keyring=keyring, storage=storage, dispatcher=dispatcher)


@pytest.fixture
def app(services):
    return create_app(services)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def api(client) -> Api:
    return Api(client)
