from datetime import UTC, date, datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, joinedload

from app.core.errors import ForbiddenError, NotFoundError
from app.core.permissions import FacilityPermission, UserRole
from app.models.doctor_working_hours import DoctorWorkingHour
from app.models.facility import Facility
from app.models.user import User
from app.schemas.doctor import WorkingHourIn
from app.services.availability import AvailabilityService, available_slot_starts
from app.services.facilities import FacilityService


class DoctorNotFoundError(NotFoundError):
    detail = "Doctor not found"


class DoctorService:
    def __init__(self, db: Session):
        self.db = db

    def list_for_facility(self, facility_id: UUID) -> list[User]:
        """Active doctors at an active facility."""
        facility = FacilityService(self.db).get_active(facility_id)
        return list(
            self.db.scalars(
                select(User)
                .where(
                    User.facility_id == facility.id,
                    User.role == UserRole.DOCTOR,
                    User.is_active.is_(True),
                )
                .order_by(User.name)
            )
        )

    def get_bookable(self, doctor_id: UUID) -> User:
        """An active doctor at an active facility, with the facility loaded.
        Anyone else is not found, so patients can't book them."""
        doctor = self.db.scalar(
            select(User)
            .join(User.facility)
            .where(
                User.id == doctor_id,
                User.role == UserRole.DOCTOR,
                User.is_active.is_(True),
                Facility.is_active.is_(True),
            )
            .options(joinedload(User.facility))
        )
        if doctor is None:
            raise DoctorNotFoundError
        return doctor

    def working_hours(self, doctor_id: UUID) -> list[DoctorWorkingHour]:
        return list(
            self.db.scalars(
                select(DoctorWorkingHour)
                .where(DoctorWorkingHour.doctor_id == doctor_id)
                .order_by(DoctorWorkingHour.weekday, DoctorWorkingHour.start_time)
            )
        )

    def facility_working_hours(
        self,
        facility: Facility,
        *,
        permissions: frozenset[FacilityPermission],
    ) -> list[DoctorWorkingHour]:
        """Every doctor's weekly hours at the facility, for its staff."""
        if not permissions & {
            FacilityPermission.SCHEDULES_MANAGE_ANY,
            FacilityPermission.SCHEDULE_MANAGE_OWN,
        }:
            raise ForbiddenError
        return list(
            self.db.scalars(
                select(DoctorWorkingHour)
                .join(DoctorWorkingHour.doctor)
                .where(User.facility_id == facility.id)
                .order_by(
                    DoctorWorkingHour.doctor_id,
                    DoctorWorkingHour.weekday,
                    DoctorWorkingHour.start_time,
                )
            )
        )

    def periods_on(self, doctor_id: UUID, weekday: int) -> list[tuple[time, time]]:
        rows = self.db.execute(
            select(DoctorWorkingHour.start_time, DoctorWorkingHour.end_time).where(
                DoctorWorkingHour.doctor_id == doctor_id,
                DoctorWorkingHour.weekday == weekday,
            )
        )
        return [(start, end) for start, end in rows]

    def availability(self, doctor_id: UUID, day: date) -> tuple[User, list[datetime]]:
        """The doctor and their free slot starts on a local date. A snapshot:
        booking re-checks the slot."""
        doctor = self.get_bookable(doctor_id)
        tz = ZoneInfo(doctor.facility.timezone)
        starts = available_slot_starts(
            day,
            self.periods_on(doctor.id, day.weekday()),
            tz,
            booked=AvailabilityService(self.db).booked_starts(doctor.id, day, tz),
            now=datetime.now(UTC),
        )
        return doctor, starts

    def replace_working_hours(
        self,
        facility: Facility,
        doctor_id: UUID,
        periods: list[WorkingHourIn],
        *,
        actor: User,
        permissions: frozenset[FacilityPermission],
    ) -> list[DoctorWorkingHour]:
        """Replace a doctor's weekly schedule. Facility admins can set anyone's;
        a doctor only their own. Existing appointments are kept."""
        may_manage = FacilityPermission.SCHEDULES_MANAGE_ANY in permissions or (
            FacilityPermission.SCHEDULE_MANAGE_OWN in permissions
            and actor.id == doctor_id
        )
        if not may_manage:
            raise ForbiddenError

        # Locks the doctor's row so two concurrent replacements run one after
        # the other instead of both inserting their periods.
        doctor = self.db.scalar(
            select(User)
            .where(
                User.id == doctor_id,
                User.facility_id == facility.id,
                User.role == UserRole.DOCTOR,
            )
            .with_for_update()
        )
        if doctor is None:
            raise DoctorNotFoundError

        self.db.execute(
            delete(DoctorWorkingHour).where(DoctorWorkingHour.doctor_id == doctor.id)
        )
        self.db.add_all(
            DoctorWorkingHour(
                doctor_id=doctor.id,
                weekday=p.weekday,
                start_time=p.start_time,
                end_time=p.end_time,
            )
            for p in periods
        )
        self.db.commit()
        return self.working_hours(doctor.id)
