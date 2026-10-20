from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdempotencyKey(Base):
    """A client-supplied key that makes booking an appointment safe to retry.
    Written in the same transaction as the appointment it creates."""

    __tablename__ = "idempotency_keys"

    __table_args__ = (
        # A concurrent duplicate request fails here instead of booking twice.
        UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    # SHA-256 of the request body; rejects a key reused for a different booking.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Set before commit; a retry returns this appointment.
    appointment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
        nullable=False,
    )
