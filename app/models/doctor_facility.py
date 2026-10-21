from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.facility import Facility
    from app.models.user import User


class DoctorFacility(Base):
    """A doctor working at a facility. A doctor can work at several, keeping
    separate working hours at each; appointments and working hours reference
    this row, so a doctor can only be booked, or keep hours, where they
    work.

    Never deleted: removing a doctor from a facility deactivates this row,
    and their history there stays."""

    __tablename__ = "doctor_facilities"

    doctor_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    facility_id: Mapped[UUID] = mapped_column(
        ForeignKey("facilities.id", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )
    # False = removed from this facility: no new bookings or hours here, and
    # the facility's admins see them as deactivated. Their other facilities
    # are unaffected.
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    # When they joined the facility; their facilities are listed oldest
    # first. Set here rather than by now(), which is the same for every row
    # a transaction adds, so the order is well defined.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )

    doctor: Mapped["User"] = relationship(back_populates="clinic_links")
    facility: Mapped["Facility"] = relationship(back_populates="doctor_links")
