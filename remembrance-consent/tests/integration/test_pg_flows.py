"""End-to-end and concurrency on PostgreSQL, running as the runtime role."""
from __future__ import annotations

import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import Actor
from app.clock import SystemClock
from app.config import Settings
from app.consent.audit import append_event, iter_events, verify_chain
from app.consent.cascade import InlineDispatcher, run_cascade
from app.consent.constants import AuditEventType, ConsentAction, RevocationStatus
from app.consent.kernel import authorize
from app.consent.models import AuthorizationToken, RevocationRequest
from app.consent.router import revoke
from app.consent.schemas import RevokeRequest
from app.consent.tokens import KeyRing
from app.db import make_engine
from app.export.manifest import verify_bundle
from app.main import create_app
from app.services import build_services
from app.storage import LocalFileStorage, UrlSigner
from scripts import verify_audit_chain
from tests.factories import seed_artifacts, seed_grant, seed_profile
from tests.support import AUTH_SECRET, BEN_A, OWNER, Api


@pytest.fixture
def pg_services(pg, tmp_path):
    engine = make_engine(pg.app_url)
    storage = LocalFileStorage(tmp_path / "objects", UrlSigner(b"u" * 40), "http://testserver")
    holder = {}
    services = build_services(
        Settings(env="test", database_url=pg.app_url, auth_jwt_key=AUTH_SECRET, authorize_rate_per_minute=6000, authorize_burst=1000),
        engine=engine,
        keyring=KeyRing.generate("pg-kid"),
        storage=storage,
        dispatcher=InlineDispatcher(lambda g: run_cascade(g, session_factory=holder["s"].session_factory, storage=storage, clock=SystemClock())),
    )
    holder["s"] = services
    yield services
    engine.dispose()


def test_full_lifecycle_on_postgres(pg, pg_services, tmp_path):
    services = pg_services
    with TestClient(create_app(services)) as client:
        api = Api(client)
        grant = api.ready_grant()
        profile_id = grant["deceased_profile_id"]
        token = api.authorize(OWNER, "INITIATE_VOICE_SYNTHESIS", profile_id, "VOICE_SYNTHESIS").json()["token"]
        seed_artifacts(services.session_factory, SystemClock(), services.storage, UUID(profile_id), UUID(grant["id"]))
        assert api.authorize(OWNER, "VIEW_MEMORIAL", profile_id, "FINANCIAL").status_code == 403

        api.revoke(grant["id"], BEN_A)

        with services.session_factory() as s:
            request = s.scalars(select(RevocationRequest)).one()
            assert request.status is RevocationStatus.COMPLETE
            assert all(t.revoked_at for t in s.scalars(select(AuthorizationToken)))
        from app.consent.tokens import TokenError

        with pytest.raises(TokenError):
            services.validator.validate(token, action=ConsentAction.INITIATE_VOICE_SYNTHESIS, profile_id=UUID(profile_id))

        url = client.post(f"/export/{profile_id}", headers=OWNER.headers()).json()["url"]
        archive = client.get(url.replace("http://testserver", "")).content
        report = verify_bundle(archive, services.keyring.verification_keys)
        assert report.ok and report.signature_checked, report.errors
        log = zipfile.ZipFile(io.BytesIO(archive)).read("audit_log.jsonl").decode().splitlines()
        kinds = [json.loads(line)["event_type"] for line in log]
        assert kinds.count("MODEL_DELETED") == 5 and "PURPOSE_VIOLATION_ATTEMPT" in kinds
        assert kinds.index("CONSENT_REVOKED") < kinds.index("REVOCATION_CASCADE_COMPLETED")
    assert verify_audit_chain.main(["--database-url", pg.app_url]) == 0


def test_concurrent_appends_keep_one_linear_chain(pg_services):
    services = pg_services
    profile_id = uuid4()
    start = threading.Barrier(8)

    def writer(worker: int) -> None:
        start.wait()
        for n in range(20):
            with services.session_factory.begin() as s:
                append_event(s, clock=SystemClock(), event_type=AuditEventType.AUTHORIZATION_DENIED, actor_id=f"w{worker}",
                             deceased_profile_id=profile_id, payload={"n": n})

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(writer, range(8)))
    with services.session_factory() as s:
        report = verify_chain(iter_events(s, profile_id))
    assert report.ok and report.chains[profile_id].length == 160


def test_no_token_outlives_a_concurrent_revocation(pg_services):
    """authorize() holds FOR SHARE on the grant; revoke() needs FOR UPDATE.
    Whichever commits first, no active token can remain afterwards."""
    services = pg_services
    clock = SystemClock()
    owner = Actor(OWNER.user_id, OWNER.email)
    def request_token(start: threading.Barrier, profile_id: UUID):
        start.wait()
        return authorize(ConsentAction.INITIATE_VOICE_SYNTHESIS, profile_id, owner, purpose_code="VOICE_SYNTHESIS", ctx=services.kernel)

    def revoke_grant(start: threading.Barrier, grant_id: UUID):
        start.wait()
        return revoke(grant_id, RevokeRequest(reason="race"), owner, services)

    outcomes = []
    for _ in range(15):
        profile_id = seed_profile(services.session_factory, clock)
        grant_id = seed_grant(services.session_factory, clock, profile_id)
        start = threading.Barrier(2)
        with ThreadPoolExecutor(2) as pool:
            token_future = pool.submit(request_token, start, profile_id)
            revoke_future = pool.submit(revoke_grant, start, grant_id)
            result, _ = token_future.result(), revoke_future.result()
        outcomes.append(result.allowed)
        with services.session_factory() as s:
            live = s.scalars(select(AuthorizationToken).where(AuthorizationToken.consent_grant_id == grant_id,
                                                              AuthorizationToken.revoked_at.is_(None))).all()
        assert live == []
    assert len(outcomes) == 15  # both interleavings are legal; neither may leave a live token


def test_timestamps_round_trip_exactly(pg_services):
    services = pg_services
    profile_id = uuid4()
    moment = datetime(2026, 9, 28, 12, 0, 0, 123456, tzinfo=UTC)

    class Fixed:
        def now(self):
            return moment

    with services.session_factory.begin() as s:
        event = append_event(s, clock=Fixed(), event_type=AuditEventType.PROFILE_CREATED, actor_id="a",
                             deceased_profile_id=profile_id, payload={})
        stored_hash = event.event_hash
    with services.session_factory() as s:
        [record] = list(iter_events(s, profile_id))
    assert record.created_at == moment and record.recomputed_hash() == stored_hash
