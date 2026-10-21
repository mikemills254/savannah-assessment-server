from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError,
    ForbiddenError,
    InvalidRequestError,
    NotFoundError,
)
from app.core.permissions import FacilityPermission, UserRole
from app.models.doctor_facility import DoctorFacility
from app.models.doctor_working_hours import DoctorWorkingHour
from app.models.facility import Facility
from app.models.user import User
from app.schemas.doctor import WorkingHourIn
from app.services.availability import AvailabilityService, local_day_bounds, shifts
from app.services.facilities import FacilityService

MAX_AVAILABILITY_DAYS = 31
# How far ahead the facility list looks for each facility's next free slot.
NEXT_AVAILABLE_DAYS = 14
# How far ahead a doctor's hours at one facility are checked against their
# hours at the others.
CLASH_CHECK_DAYS = 7

WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


class DoctorNotFoundError(NotFoundError):
    detail = "Doctor not found"


class HoursClashError(ConflictError):
    detail = "These hours overlap the doctor's hours at another facility"


@dataclass(frozen=True)
class FacilityAvailability:
    facility: Facility
    dates: list[date]
    # Bookable doctors by name, each with their free slot starts per date
    # (every date, in order, empty when nothing is free).
    doctors: list[tuple[User, dict[date, list[datetime]]]]


@dataclass(frozen=True)
class DoctorAvailability:
    doctor: User
    # Each facility the doctor can be booked at that was asked about, with
    # their free slot starts there on the date.
    by_facility: list[tuple[Facility, list[datetime]]]


class DoctorService:
    def __init__(self, db: Session):
        self.db = db

    def list_for_facility(self, facility_id: UUID) -> list[User]:
        """Active doctors at an active facility."""
        facility = FacilityService(self.db).get_active(facility_id)
        return [doctor for _, doctor in self._active_at([facility.id])]

    def _active_at(self, facility_ids: list[UUID]) -> list[tuple[UUID, User]]:
        """(facility id, doctor) for each active doctor at the facilities,
        by name. Whether the facilities are active is the caller's check."""
        rows = self.db.execute(
            select(DoctorFacility.facility_id, User)
            .join(User, User.id == DoctorFacility.doctor_id)
            .where(
                DoctorFacility.facility_id.in_(facility_ids),
                DoctorFacility.is_active.is_(True),
                User.role == UserRole.DOCTOR,
                User.is_active.is_(True),
            )
            .order_by(User.name, User.id)
        )
        return [(facility_id, doctor) for facility_id, doctor in rows]

    def bookable_facilities(
        self, doctor_id: UUID, facility_id: UUID | None = None
    ) -> tuple[User, list[Facility]]:
        """An active doctor and the active facilities they're active at,
        oldest link first (just `facility_id`, if given). Not found if there
        are none, so patients can't book them."""
        query = (
            select(User, Facility)
            .join(DoctorFacility, DoctorFacility.doctor_id == User.id)
            .join(Facility, Facility.id == DoctorFacility.facility_id)
            .where(
                User.id == doctor_id,
                User.role == UserRole.DOCTOR,
                User.is_active.is_(True),
                DoctorFacility.is_active.is_(True),
                Facility.is_active.is_(True),
            )
            .order_by(DoctorFacility.created_at, Facility.id)
        )
        if facility_id is not None:
            query = query.where(Facility.id == facility_id)
        rows = self.db.execute(query).all()
        if not rows:
            raise DoctorNotFoundError
        return rows[0][0], [facility for _, facility in rows]

    def get_bookable(self, doctor_id: UUID, facility: Facility) -> User:
        """The doctor, if they can be booked at the facility."""
        doctor, _ = self.bookable_facilities(doctor_id, facility.id)
        return doctor

    def working_hours(
        self, doctor_id: UUID, facility_id: UUID
    ) -> list[DoctorWorkingHour]:
        return list(
            self.db.scalars(
                select(DoctorWorkingHour)
                .where(
                    DoctorWorkingHour.doctor_id == doctor_id,
                    DoctorWorkingHour.facility_id == facility_id,
                )
                .order_by(DoctorWorkingHour.weekday, DoctorWorkingHour.start_time)
            )
        )

    def facility_working_hours(
        self,
        facility: Facility,
        *,
        permissions: frozenset[FacilityPermission],
    ) -> list[DoctorWorkingHour]:
        """Every doctor's weekly hours at the facility, for its staff. Hours
        a doctor keeps at other facilities aren't included."""
        if not permissions & {
            FacilityPermission.SCHEDULES_MANAGE_ANY,
            FacilityPermission.SCHEDULE_MANAGE_OWN,
        }:
            raise ForbiddenError
        return list(
            self.db.scalars(
                select(DoctorWorkingHour)
                .where(DoctorWorkingHour.facility_id == facility.id)
                .order_by(
                    DoctorWorkingHour.doctor_id,
                    DoctorWorkingHour.weekday,
                    DoctorWorkingHour.start_time,
                )
            )
        )

    def periods_on(
        self, doctor_id: UUID, facility_id: UUID, weekday: int
    ) -> list[tuple[time, time]]:
        rows = self.db.execute(
            select(DoctorWorkingHour.start_time, DoctorWorkingHour.end_time).where(
                DoctorWorkingHour.doctor_id == doctor_id,
                DoctorWorkingHour.facility_id == facility_id,
                DoctorWorkingHour.weekday == weekday,
            )
        )
        return [(start, end) for start, end in rows]

    def availability(
        self, doctor_id: UUID, day: date, *, facility_id: UUID | None = None
    ) -> DoctorAvailability:
        """The doctor's free slot starts on a local date at each facility
        they can be booked at (or just `facility_id`). A snapshot: booking
        re-checks the slot. A booking at one facility also blocks the same
        time at the others."""
        doctor, facilities = self.bookable_facilities(doctor_id, facility_id)
        bounds = [local_day_bounds(day, ZoneInfo(f.timezone)) for f in facilities]
        schedules = AvailabilityService(self.db).schedules(
            [doctor.id], min(b[0] for b in bounds), max(b[1] for b in bounds)
        )
        now = datetime.now(UTC)
        return DoctorAvailability(
            doctor=doctor,
            by_facility=[
                (
                    f,
                    schedules.free_slots(
                        doctor.id, f.id, day, ZoneInfo(f.timezone), now=now
                    ),
                )
                for f in facilities
            ],
        )

    def facility_availability(
        self, facility_id: UUID, *, first: date | None, days: int
    ) -> FacilityAvailability:
        """Free slot starts of every bookable doctor at the facility, for
        `days` local dates from `first` (default: today at the facility).
        The same slots availability() gives one doctor for one date, from
        four queries however many doctors and days."""
        if not 1 <= days <= MAX_AVAILABILITY_DAYS:
            raise InvalidRequestError(
                f"days must be between 1 and {MAX_AVAILABILITY_DAYS}"
            )
        facility = FacilityService(self.db).get_active(facility_id)
        tz = ZoneInfo(facility.timezone)
        now = datetime.now(UTC)
        today = now.astimezone(tz).date()
        if first is None:
            first = today
        elif first < today:
            raise InvalidRequestError(
                f"from can't be before today at the facility ({today.isoformat()})"
            )

        dates = [first + timedelta(days=i) for i in range(days)]
        doctors = [doctor for _, doctor in self._active_at([facility.id])]
        schedules = AvailabilityService(self.db).schedules(
            [d.id for d in doctors],
            local_day_bounds(dates[0], tz)[0],
            local_day_bounds(dates[-1], tz)[1],
        )
        return FacilityAvailability(
            facility=facility,
            dates=dates,
            doctors=[
                (
                    doctor,
                    {
                        day: schedules.free_slots(
                            doctor.id, facility.id, day, tz, now=now
                        )
                        for day in dates
                    },
                )
                for doctor in doctors
            ],
        )

    def next_available_by_facility(
        self, facilities: list[Facility]
    ) -> dict[UUID, tuple[User, datetime] | None]:
        """Each facility's earliest free slot in the next 14 days at the
        facility, with its doctor, or None. Three queries however many
        facilities and doctors, and a facility's search stops at the first
        day with a free slot."""
        if not facilities:
            return {}
        doctors_at: dict[UUID, list[User]] = {}
        for facility_id, doctor in self._active_at([f.id for f in facilities]):
            doctors_at.setdefault(facility_id, []).append(doctor)

        now = datetime.now(UTC)
        dates_at: dict[UUID, list[date]] = {}
        for facility in facilities:
            today = now.astimezone(ZoneInfo(facility.timezone)).date()
            dates_at[facility.id] = [
                today + timedelta(days=i) for i in range(NEXT_AVAILABLE_DAYS)
            ]
        # Past slots are never free, so the bookings that matter start from now.
        end = max(
            local_day_bounds(dates_at[f.id][-1], ZoneInfo(f.timezone))[1]
            for f in facilities
        )
        schedules = AvailabilityService(self.db).schedules(
            {d.id for doctors in doctors_at.values() for d in doctors}, now, end
        )

        result: dict[UUID, tuple[User, datetime] | None] = {}
        for facility in facilities:
            tz = ZoneInfo(facility.timezone)
            result[facility.id] = None
            for day in dates_at[facility.id]:
                firsts = []
                for doctor in doctors_at.get(facility.id, []):
                    starts = schedules.free_slots(
                        doctor.id, facility.id, day, tz, now=now
                    )
                    if starts:
                        firsts.append((starts[0], doctor))
                if firsts:
                    # Doctors are in name order and min keeps the first of
                    # equals, so a tie goes to the first by name.
                    start, doctor = min(firsts, key=lambda pair: pair[0])
                    result[facility.id] = (doctor, start)
                    break
        return result

    def replace_working_hours(
        self,
        facility: Facility,
        doctor_id: UUID,
        periods: list[WorkingHourIn],
        *,
        actor: User,
        permissions: frozenset[FacilityPermission],
    ) -> list[DoctorWorkingHour]:
        """Replace a doctor's weekly schedule at the facility. Facility admins
        can set anyone's; a doctor only their own. Existing appointments are
        kept, and the doctor's hours at other facilities are untouched, but
        mustn't overlap these (409): a doctor can't be in two places."""
        may_manage = FacilityPermission.SCHEDULES_MANAGE_ANY in permissions or (
            FacilityPermission.SCHEDULE_MANAGE_OWN in permissions
            and actor.id == doctor_id
        )
        if not may_manage:
            raise ForbiddenError

        # Locks the doctor's row so two concurrent replacements, at this
        # facility or another, run one after the other: each then sees the
        # other's hours when checking for overlaps.
        doctor = self.db.scalar(
            select(User)
            .join(DoctorFacility, DoctorFacility.doctor_id == User.id)
            .where(
                User.id == doctor_id,
                User.role == UserRole.DOCTOR,
                DoctorFacility.facility_id == facility.id,
            )
            .with_for_update(of=User)
        )
        if doctor is None:
            raise DoctorNotFoundError

        self._check_no_clash(
            doctor, facility, periods, show_facility=actor.id == doctor.id
        )

        self.db.execute(
            delete(DoctorWorkingHour).where(
                DoctorWorkingHour.doctor_id == doctor.id,
                DoctorWorkingHour.facility_id == facility.id,
            )
        )
        self.db.add_all(
            DoctorWorkingHour(
                doctor_id=doctor.id,
                facility_id=facility.id,
                weekday=p.weekday,
                start_time=p.start_time,
                end_time=p.end_time,
            )
            for p in periods
        )
        self.db.commit()
        return self.working_hours(doctor.id, facility.id)

    def _check_no_clash(
        self,
        doctor: User,
        facility: Facility,
        periods: list[WorkingHourIn],
        *,
        show_facility: bool,
    ) -> None:
        """Refuse hours that overlap the doctor's hours at another facility
        where they're active, compared as real instants over the coming week,
        so facilities in different timezones are compared correctly. Only
        the doctor is told which facility: an admin of this one needn't know
        where else they work."""
        others = self.db.execute(
            select(
                Facility.name,
                Facility.timezone,
                DoctorWorkingHour.weekday,
                DoctorWorkingHour.start_time,
                DoctorWorkingHour.end_time,
            )
            .join(Facility, Facility.id == DoctorWorkingHour.facility_id)
            .join(
                DoctorFacility,
                (DoctorFacility.doctor_id == DoctorWorkingHour.doctor_id)
                & (DoctorFacility.facility_id == DoctorWorkingHour.facility_id),
            )
            .where(
                DoctorWorkingHour.doctor_id == doctor.id,
                DoctorWorkingHour.facility_id != facility.id,
                DoctorFacility.is_active.is_(True),
            )
        ).all()
        if not others or not periods:
            return

        tz = ZoneInfo(facility.timezone)
        today = datetime.now(UTC).astimezone(tz).date()
        mine = shifts(
            [(p.weekday, p.start_time, p.end_time) for p in periods],
            tz,
            today,
            CLASH_CHECK_DAYS,
        )
        for name, timezone, weekday, start, end in others:
            # A day either side, so a shift that crosses midnight between the
            # two timezones is still compared.
            theirs = shifts(
                [(weekday, start, end)],
                ZoneInfo(timezone),
                today - timedelta(days=1),
                CLASH_CHECK_DAYS + 2,
            )
            for shift in mine:
                if any(shift.overlaps(other) for other in theirs):
                    when = (
                        f"{WEEKDAYS[shift.weekday]} "
                        f"{shift.start_time:%H:%M}-{shift.end_time:%H:%M}"
                    )
                    where = (
                        f"your hours at {name}"
                        if show_facility
                        else ("the doctor's hours at another facility")
                    )
                    raise HoursClashError(f"{when} overlaps {where}")
