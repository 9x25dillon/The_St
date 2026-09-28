"""Celery worker: the revocation cascade and its sweeper.

    celery -A app.worker worker --loglevel=INFO
    celery -A app.worker beat   --loglevel=INFO     # runs the sweeper

Delivery is at-least-once (acks_late + reject_on_worker_lost); the cascade
is idempotent, so duplicates are harmless. Transient failures retry forever
with capped exponential backoff: biometric deletion must eventually happen.
Alert on revocation_request.attempts growing or last_error persisting.
"""
from __future__ import annotations

from uuid import UUID

from celery import Celery

from app.config import Settings
from app.consent.cascade import PermanentCascadeError, run_cascade, sweep_stale_revocations

SWEEP_INTERVAL_SECONDS = 300.0

celery_app = Celery("remembrance")
_services = None


def configure(settings: Settings) -> Celery:
    celery_app.conf.update(
        broker_url=settings.celery_broker_url,
        task_serializer="json",
        accept_content=["json"],
        result_backend=None,
        task_ignore_result=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        beat_schedule={
            "sweep-stale-revocations": {"task": "consent.sweep_revocations", "schedule": SWEEP_INTERVAL_SECONDS}
        },
    )
    return celery_app


def set_services(services) -> None:
    """Inject services (tests, or a custom bootstrap)."""
    global _services
    _services = services


def services():
    global _services
    if _services is None:  # pragma: no cover - production bootstrap
        from app.services import build_services

        _services = build_services(Settings())
    return _services


@celery_app.task(
    name="consent.cascade_revocation",
    autoretry_for=(Exception,),
    dont_autoretry_for=(PermanentCascadeError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=None,
)
def cascade_revocation(consent_grant_id: str) -> dict[str, object]:
    s = services()
    return run_cascade(UUID(consent_grant_id), session_factory=s.session_factory, storage=s.storage, clock=s.clock).to_json()


@celery_app.task(name="consent.sweep_revocations")
def sweep_revocations() -> list[str]:
    s = services()
    ids = sweep_stale_revocations(
        session_factory=s.session_factory,
        clock=s.clock,
        dispatcher=s.dispatcher,
        stale_after_seconds=s.settings.cascade_stale_after_seconds,
    )
    return [str(i) for i in ids]


if not celery_app.conf.broker_url:  # pragma: no cover - import-time default for the CLI
    configure(Settings())
