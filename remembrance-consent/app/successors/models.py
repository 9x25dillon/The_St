from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.types import UTCDateTime


class SuccessorDesignation(Base):
    """A person who inherits rights over a profile (revoke, export, erase,
    and use of a SELF_PRE_NEED grant after the subject's death).
    priority_order is the contact order when several successors exist."""

    __tablename__ = "successor_designation"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    deceased_profile_id: Mapped[UUID] = mapped_column(ForeignKey("deceased_profile.id", ondelete="RESTRICT"), index=True)
    successor_user_id: Mapped[str] = mapped_column(String(255))
    priority_order: Mapped[int] = mapped_column(Integer)
    designated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    designated_by: Mapped[str] = mapped_column(String(255))

    __table_args__ = (
        UniqueConstraint("deceased_profile_id", "priority_order", name="uq_successor_profile_priority"),
        UniqueConstraint("deceased_profile_id", "successor_user_id", name="uq_successor_profile_user"),
        CheckConstraint("priority_order >= 1", name="priority_positive"),
    )
