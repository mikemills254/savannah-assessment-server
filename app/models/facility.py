from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.user import User


class Facility(Base):
    """A clinic or hospital. Its admins and doctors are users whose
    facility_id points here."""

    __tablename__ = "facilities"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )
    # IANA name, e.g. "Africa/Nairobi". Doctors' working hours are in this
    # local time.
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    # False = deactivated: hidden from patients, and its staff can't manage it.
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    staff: Mapped[list["User"]] = relationship(
        back_populates="facility",
    )
    appointments: Mapped[list["Appointment"]] = relationship(
        back_populates="facility",
    )
