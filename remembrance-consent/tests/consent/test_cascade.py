"""Revocation cascade: completeness, idempotence, partial-failure retry."""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

import app.consent.cascade as cascade_module
from app.consent.audit import iter_events, verify_chain
from app.consent.cascade import (
    CASCADE_ACTOR,
    CascadeError,
    CeleryDispatcher,
    InlineDispatcher,
    PermanentCascadeError,
    run_cascade,
    sweep_stale_revocations,
)
from app.consent.constants import AuditEventType, RevocationStatus
from app.consent.models import AuditEvent, AuthorizationToken, ConsentGrant, RevocationRequest
from app.downstream.models import AudioArtifact, DeliveryStatus, ScheduledDelivery, VoiceModel
from tests.conftest import RecordingDispatcher
from tests.factories import seed_artifacts, seed_grant, seed_profile, seed_token


class FlakyStorage:
    """Wraps real storage; fails the first `failures` deletes of chosen keys."""

    def __init__(self, inner, fail_keys=(), failures=1, after_delete=None):
        self.inner = inner
        self.remaining = {key: failures for key in fail_keys}
        self.deleted: list[str] = []
        self.after_delete = after_delete

    def delete(self, key):
        if self.remaining.get(key, 0) > 0:
            self.remaining[key] -= 1
            raise OSError(f"object store unavailable for {key}")
        self.inner.delete(key)
        self.deleted.append(key)
        if self.after_delete:
            self.after_delete(key)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def revoke(session_factory, clock, grant_id, *, with_request=True):
    with session_factory.begin() as s:
        grant = s.get(ConsentGrant, grant_id)
        grant.is_active = False
        grant.revoked_at = clock.now()
        grant.revocation_reason = "family decision"
        if with_request:
            s.add(RevocationRequest(consent_grant_id=grant_id, requested_by="user-owner", reason="family decision",
                                    status=RevocationStatus.PENDING, created_at=clock.now(), updated_at=clock.now(), attempts=0))


@pytest.fixture
def world(session_factory, clock, storage):
    profile_id = seed_profile(session_factory, clock)
    grant_id = seed_grant(session_factory, clock, profile_id)
    other_grant = seed_grant(session_factory, clock, profile_id, grantor="user-other")
    ids = seed_artifacts(session_factory, clock, storage, profile_id, grant_id, other_grant_id=other_grant)
    tokens = [seed_token(session_factory, clock, profile_id, grant_id) for _ in range(2)]
    revoke(session_factory, clock, grant_id)
    return {"profile": profile_id, "grant": grant_id, "other": other_grant, "tokens": tokens, **ids}


def audit_types(session_factory, grant_id):
    with session_factory() as s:
        return [e.event_type for e in s.scalars(select(AuditEvent).where(AuditEvent.consent_grant_id == grant_id).order_by(AuditEvent.id))]


def request_row(session_factory, grant_id) -> RevocationRequest:
    with session_factory() as s:
        return s.scalars(select(RevocationRequest).where(RevocationRequest.consent_grant_id == grant_id)).one()


def assert_fully_cascaded(session_factory, storage, world):
    with session_factory() as s:
        for model in (VoiceModel, AudioArtifact):
            for row in s.scalars(select(model)).all():
                if row.id in world["models"] + world["audio"]:
                    assert row.deleted_at is not None and row.storage_key is None
        statuses = {d.id: d.status for d in s.scalars(select(ScheduledDelivery))}
        assert statuses[world["scheduled"][0]] is DeliveryStatus.CANCELLED
        assert statuses[world["sent"][0]] is DeliveryStatus.SENT  # history is not rewritten
        assert all(t.revoked_at is not None for t in s.scalars(select(AuthorizationToken)))
    assert not any(storage.exists(k) for k in _keys(world))


def _keys(world):
    grant = world["grant"]
    return [f"models/{grant}/{n}.bin" for n in range(2)] + [f"audio/{grant}/{n}.wav" for n in range(3)]


def test_full_cascade(session_factory, clock, storage, world):
    report = run_cascade(world["grant"], session_factory=session_factory, storage=storage, clock=clock)

    assert report.to_json() == {
        "consent_grant_id": str(world["grant"]),
        "already_complete": False,
        "tokens_revoked": 2,
        "deliveries_cancelled": 1,
        "audio_artifacts_deleted": 3,  # includes audio tagged with another grant but made from this grant's model
        "voice_models_deleted": 2,
    }
    assert_fully_cascaded(session_factory, storage, world)
    request = request_row(session_factory, world["grant"])
    assert request.status is RevocationStatus.COMPLETE and request.cascade_completed_at is not None and request.attempts == 1
    types = audit_types(session_factory, world["grant"])
    assert types.count(AuditEventType.MODEL_DELETED) == 5
    assert types.count(AuditEventType.DELIVERY_CANCELLED) == 1
    assert types[-1] is AuditEventType.REVOCATION_CASCADE_COMPLETED
    with session_factory() as s:
        deleted = s.scalars(select(AuditEvent).where(AuditEvent.event_type == AuditEventType.MODEL_DELETED)).all()
        assert {e.payload["artifact_kind"] for e in deleted} == {"VOICE_MODEL", "AUDIO_ARTIFACT"}
        assert all(e.actor_id == CASCADE_ACTOR and len(e.payload["storage_key_sha256"]) == 64 for e in deleted)
        assert verify_chain(iter_events(s)).ok


def test_rerun_is_a_no_op(session_factory, clock, storage, world):
    run_cascade(world["grant"], session_factory=session_factory, storage=storage, clock=clock)
    before = audit_types(session_factory, world["grant"])
    again = run_cascade(world["grant"], session_factory=session_factory, storage=storage, clock=clock)
    assert again.already_complete and audit_types(session_factory, world["grant"]) == before
    assert request_row(session_factory, world["grant"]).attempts == 1


def test_partial_failure_resumes_without_duplicate_events(session_factory, clock, storage, world):
    flaky = FlakyStorage(storage, fail_keys=[f"audio/{world['grant']}/1.wav"])
    with pytest.raises(OSError):
        run_cascade(world["grant"], session_factory=session_factory, storage=flaky, clock=clock)

    request = request_row(session_factory, world["grant"])
    assert request.status is RevocationStatus.PROCESSING and "object store unavailable" in request.last_error
    with session_factory() as s:
        done = s.scalar(select(func.count()).select_from(AudioArtifact).where(AudioArtifact.deleted_at.is_not(None)))
        failed = s.scalars(select(AudioArtifact).where(AudioArtifact.storage_key == f"audio/{world['grant']}/1.wav")).one()
    assert failed.deleted_at is None  # the failing artifact is untouched
    assert audit_types(session_factory, world["grant"]).count(AuditEventType.MODEL_DELETED) == done  # one event per tombstone

    clock.advance(minutes=1)
    report = run_cascade(world["grant"], session_factory=session_factory, storage=flaky, clock=clock)
    assert report.audio_artifacts_deleted == 3 - done and report.voice_models_deleted == 2
    assert_fully_cascaded(session_factory, storage, world)
    assert audit_types(session_factory, world["grant"]).count(AuditEventType.MODEL_DELETED) == 5
    request = request_row(session_factory, world["grant"])
    assert request.status is RevocationStatus.COMPLETE and request.attempts == 2 and request.last_error is None


def test_crash_after_object_delete_before_commit_is_repaired(session_factory, clock, storage, world, monkeypatch):
    real_append = cascade_module.append_event
    calls = {"n": 0}

    def crash_on_first_model_deleted(session, **kwargs):
        if kwargs["event_type"] is AuditEventType.MODEL_DELETED and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("worker lost")
        return real_append(session, **kwargs)

    monkeypatch.setattr(cascade_module, "append_event", crash_on_first_model_deleted)
    with pytest.raises(RuntimeError):
        run_cascade(world["grant"], session_factory=session_factory, storage=storage, clock=clock)
    with session_factory() as s:
        live = s.scalars(select(AudioArtifact).where(AudioArtifact.deleted_at.is_(None))).all()
    orphaned = [a for a in live if not storage.exists(a.storage_key)]
    assert len(live) == 3 and len(orphaned) == 1  # row rolled back, object already gone

    run_cascade(world["grant"], session_factory=session_factory, storage=storage, clock=clock)
    assert_fully_cascaded(session_factory, storage, world)
    assert audit_types(session_factory, world["grant"]).count(AuditEventType.MODEL_DELETED) == 5


def test_artifact_written_mid_cascade_blocks_completion_until_retry(session_factory, clock, storage, world):
    late = {}

    def downstream_race(key):
        if key.startswith("models/") and not late:
            with session_factory.begin() as s:
                artifact = AudioArtifact(id=uuid4(), consent_grant_id=world["grant"], storage_key="audio/late.wav", created_at=clock.now())
                s.add(artifact)
                late["id"] = artifact.id
            storage.put("audio/late.wav", b"x", "audio/wav")

    racing = FlakyStorage(storage, after_delete=downstream_race)
    with pytest.raises(CascadeError, match="incomplete"):
        run_cascade(world["grant"], session_factory=session_factory, storage=racing, clock=clock)
    assert request_row(session_factory, world["grant"]).status is RevocationStatus.PROCESSING

    run_cascade(world["grant"], session_factory=session_factory, storage=racing, clock=clock)
    assert not storage.exists("audio/late.wav")
    assert request_row(session_factory, world["grant"]).status is RevocationStatus.COMPLETE


def test_grants_that_cannot_cascade(session_factory, clock, storage):
    profile_id = seed_profile(session_factory, clock)
    active = seed_grant(session_factory, clock, profile_id)
    with pytest.raises(PermanentCascadeError, match="not revoked"):
        run_cascade(active, session_factory=session_factory, storage=storage, clock=clock)
    with pytest.raises(PermanentCascadeError, match="unknown"):
        run_cascade(uuid4(), session_factory=session_factory, storage=storage, clock=clock)


def test_out_of_band_revocation_gets_a_request(session_factory, clock, storage):
    profile_id = seed_profile(session_factory, clock)
    grant_id = seed_grant(session_factory, clock, profile_id)
    revoke(session_factory, clock, grant_id, with_request=False)
    run_cascade(grant_id, session_factory=session_factory, storage=storage, clock=clock)
    request = request_row(session_factory, grant_id)
    assert request.requested_by == CASCADE_ACTOR and request.status is RevocationStatus.COMPLETE


def test_sweeper_redispatches_only_stale_open_requests(session_factory, clock, storage, world):
    profile_id = world["profile"]
    fresh_grant = seed_grant(session_factory, clock, profile_id, grantor="user-fresh")
    done_grant = seed_grant(session_factory, clock, profile_id, grantor="user-done")
    revoke(session_factory, clock, done_grant)
    run_cascade(done_grant, session_factory=session_factory, storage=storage, clock=clock)
    clock.advance(minutes=30)
    revoke(session_factory, clock, fresh_grant)
    dispatcher = RecordingDispatcher()

    swept = sweep_stale_revocations(session_factory=session_factory, clock=clock, dispatcher=dispatcher, stale_after_seconds=600)

    assert swept == [world["grant"]] and dispatcher.dispatched == [world["grant"]]


def test_inline_dispatcher_runs_and_swallows_failures(caplog):
    seen = []
    InlineDispatcher(seen.append).dispatch(grant := uuid4())
    assert seen == [grant]

    def boom(_):
        raise RuntimeError("nope")

    InlineDispatcher(boom).dispatch(uuid4())
    assert "sweeper will retry" in caplog.text


def test_celery_dispatcher_enqueues(monkeypatch):
    import app.worker as worker

    sent = []
    monkeypatch.setattr(worker.cascade_revocation, "apply_async", lambda args: sent.append(args))
    CeleryDispatcher().dispatch(grant := uuid4())
    assert sent == [[str(grant)]]


def test_celery_tasks_run_eagerly(services, session_factory, clock, storage, world):
    import app.worker as worker

    worker.configure(services.settings)
    worker.celery_app.conf.task_always_eager = True
    worker.celery_app.conf.task_eager_propagates = True
    worker.set_services(services)
    try:
        result = worker.cascade_revocation.delay(str(world["grant"])).get()
        assert result["voice_models_deleted"] == 2
        with pytest.raises(PermanentCascadeError):
            worker.cascade_revocation.delay(str(uuid4())).get()
        clock.advance(hours=1)
        assert worker.sweep_revocations.delay().get() == []
    finally:
        worker.celery_app.conf.task_always_eager = False
        worker.set_services(None)
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(RevocationRequest).where(RevocationRequest.status != RevocationStatus.COMPLETE)) == 0


def test_revoked_tokens_stay_revoked_after_cascade(session_factory, clock, storage, world):
    run_cascade(world["grant"], session_factory=session_factory, storage=storage, clock=clock)
    with session_factory() as s:
        revoked_at = {t.jti: t.revoked_at for t in s.scalars(select(AuthorizationToken))}
    assert all(v is not None and v <= clock.now() + timedelta(seconds=1) for v in revoked_at.values())
