from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SQLAlchemyEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.permissions import UserRole
from app.db.base import Base

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.doctor_working_hours import DoctorWorkingHour
    from app.models.facility import Facility


class User(Base):
    """A person with exactly one role. Facility admins and doctors belong to
    one facility; super admins and patients to none."""

    __tablename__ = "users"

    __table_args__ = (
        CheckConstraint(
            "(role IN ('FACILITY_ADMIN', 'DOCTOR')) = (facility_id IS NOT NULL)",
            name="ck_user_staff_has_facility",
        ),
        # Lets appointments reference (doctor_id, facility_id) together, so a
        # doctor can only be booked at their own facility.
        UniqueConstraint(
            "id",
            "facility_id",
            name="uq_user_id_facility",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Stored lowercased; see AuthService.normalize_email.
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    # Null until a password is set: an invited user who hasn't accepted yet,
    # or someone who only signs in with Google. Neither can log in with a
    # password; an invite lets them set one.
    hashed_password: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    # Google's account id ("sub"), set on the first Google sign-in.
    google_sub: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=True,
    )
    role: Mapped[UserRole] = mapped_column(
        SQLAlchemyEnum(
            UserRole,
            name="user_role",
            native_enum=True,
        ),
        default=UserRole.PATIENT,
        nullable=False,
    )
    # Set for FACILITY_ADMIN and DOCTOR, null otherwise (see the check
    # constraint). Never changes: a doctor's appointments are tied to it.
    facility_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("facilities.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    # False = deactivated: can't log in, and a doctor takes no new bookings.
    # History is kept.
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

    facility: Mapped["Facility | None"] = relationship(
        back_populates="staff",
    )
    # Appointments this user booked as a patient.
    appointments: Mapped[list["Appointment"]] = relationship(
        back_populates="patient",
        foreign_keys="Appointment.patient_id",
    )
    # For a doctor: appointments booked with them, and their working hours.
    doctor_appointments: Mapped[list["Appointment"]] = relationship(
        back_populates="doctor",
        foreign_keys="Appointment.doctor_id",
        overlaps="appointments,facility",
    )
    working_hours: Mapped[list["DoctorWorkingHour"]] = relationship(
        back_populates="doctor",
    )
