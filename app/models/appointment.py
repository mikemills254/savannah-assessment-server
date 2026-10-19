from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLAlchemyEnum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.facility import Facility
    from app.models.user import User


class AppointmentStatus(str, Enum):
    BOOKED = "BOOKED"
    CANCELLED = "CANCELLED"


class Appointment(Base):
    __tablename__ = "appointments"

    __table_args__ = (
        # The doctor must belong to this appointment's facility.
        ForeignKeyConstraint(
            ["doctor_id", "facility_id"],
            ["users.id", "users.facility_id"],
            ondelete="RESTRICT",
            name="fk_appointment_doctor_in_facility",
        ),
        CheckConstraint(
            "ends_at > starts_at",
            name="ck_appointment_valid_period",
        ),
        Index(
            "uq_active_doctor_slot",
            "doctor_id",
            "starts_at",
            unique=True,
            postgresql_where=text("status = 'BOOKED'"),
        ),
        # A patient can't hold two bookings at the same time at one facility,
        # e.g. with two of its doctors.
        Index(
            "uq_active_patient_slot",
            "facility_id",
            "patient_id",
            "starts_at",
            unique=True,
            postgresql_where=text("status = 'BOOKED'"),
        ),
        Index(
            "ix_appointments_patient_starts_at",
            "patient_id",
            "starts_at",
        ),
        Index(
            "ix_appointments_facility_starts_at",
            "facility_id",
            "starts_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    facility_id: Mapped[UUID] = mapped_column(
        ForeignKey("facilities.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # The doctor's user id; the foreign key is the composite constraint above.
    doctor_id: Mapped[UUID] = mapped_column(
        nullable=False,
    )

    patient_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Who made the booking: the patient themselves, or a facility admin on
    # their behalf. Null for bookings made before this was recorded.
    booked_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_appointments_booked_by"),
        nullable=True,
    )

    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    status: Mapped[AppointmentStatus] = mapped_column(
        SQLAlchemyEnum(
            AppointmentStatus,
            name="appointment_status",
            native_enum=True,
        ),
        default=AppointmentStatus.BOOKED,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Who cancelled: the patient, the doctor, or a facility admin. Null while
    # booked, and for cancellations made before this was recorded.
    cancelled_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_appointments_cancelled_by"),
        nullable=True,
    )

    facility: Mapped["Facility"] = relationship(
        back_populates="appointments",
    )

    doctor: Mapped["User"] = relationship(
        back_populates="doctor_appointments",
        foreign_keys=[doctor_id, facility_id],
        overlaps="appointments,facility",
    )

    patient: Mapped["User"] = relationship(
        back_populates="appointments",
        foreign_keys=[patient_id],
    )

    booked_by: Mapped["User | None"] = relationship(
        foreign_keys=[booked_by_id],
    )

    cancelled_by: Mapped["User | None"] = relationship(
        foreign_keys=[cancelled_by_id],
    )
