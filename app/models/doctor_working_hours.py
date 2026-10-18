from datetime import time
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class DoctorWorkingHour(Base):
    """A recurring weekly working period for a doctor, in their facility's
    local time."""

    __tablename__ = "doctor_working_hours"

    __table_args__ = (
        CheckConstraint(
            "weekday >= 0 AND weekday <= 6",
            name="ck_working_hours_weekday",
        ),
        CheckConstraint(
            "start_time < end_time",
            name="ck_working_hours_valid_period",
        ),
        Index(
            "ix_working_hours_doctor_weekday",
            "doctor_id",
            "weekday",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # The doctor's user id.
    doctor_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Monday = 0, Sunday = 6
    weekday: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
    )

    start_time: Mapped[time] = mapped_column(
        Time,
        nullable=False,
    )
    end_time: Mapped[time] = mapped_column(
        Time,
        nullable=False,
    )

    doctor: Mapped["User"] = relationship(
        back_populates="working_hours",
    )