"""Import every mapped module so Base.metadata is complete (Alembic, tests)."""
from app.consent.models import (  # noqa: F401
    AuditEvent,
    AuthorizationToken,
    BeneficiaryAcknowledgment,
    ConsentGrant,
    DeceasedProfile,
    GrantBeneficiary,
    RevocationRequest,
)
from app.downstream.models import AudioArtifact, ScheduledDelivery, VoiceModel  # noqa: F401
from app.successors.models import SuccessorDesignation  # noqa: F401
