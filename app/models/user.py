from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
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
    from app.models.doctor_facility import DoctorFacility
    from app.models.doctor_working_hours import DoctorWorkingHour
    from app.models.facility import Facility


class User(Base):
    """A person with exactly one role. A facility admin belongs to one
    facility (facility_id); a doctor works at one or more (clinic_links);
    super admins and patients belong to none."""

    __tablename__ = "users"

    __table_args__ = (
        CheckConstraint(
            "(role = 'FACILITY_ADMIN') = (facility_id IS NOT NULL)",
            name="ck_user_admin_has_facility",
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
    # A FACILITY_ADMIN's facility; null for everyone else (see the check
    # constraint). Doctors' facilities are their clinic_links.
    facility_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("facilities.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    # False = deactivated: can't log in, and a doctor takes no new bookings.
    # History is kept. A doctor is deactivated when removed from their last
    # active facility.
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
    # For a doctor: the facilities they work at (active or not), the
    # appointments booked with them, and their working hours.
    clinic_links: Mapped[list["DoctorFacility"]] = relationship(
        back_populates="doctor",
        order_by="DoctorFacility.created_at",
    )
    doctor_appointments: Mapped[list["Appointment"]] = relationship(
        back_populates="doctor",
        foreign_keys="Appointment.doctor_id",
    )
    working_hours: Mapped[list["DoctorWorkingHour"]] = relationship(
        back_populates="doctor",
        foreign_keys="DoctorWorkingHour.doctor_id",
    )

    @property
    def has_password(self) -> bool:
        return self.hashed_password is not None

    @property
    def facilities(self) -> list["Facility"]:
        """Where a FACILITY_ADMIN or DOCTOR works: an admin's facility (active
        or not), or every facility a doctor is active at, oldest link first.
        Empty for everyone else. Load `facility` and `clinic_links` with the
        user (see WITH_FACILITIES) to avoid queries when this is read."""
        if self.role == UserRole.DOCTOR:
            return [link.facility for link in self.clinic_links if link.is_active]
        return [self.facility] if self.facility is not None else []

    def works_at(self, facility_id: UUID) -> bool:
        """Whether this is staff of the facility: its admin, or a doctor
        active there."""
        if self.role == UserRole.DOCTOR:
            return any(
                link.facility_id == facility_id and link.is_active
                for link in self.clinic_links
            )
        return self.role == UserRole.FACILITY_ADMIN and self.facility_id == facility_id
