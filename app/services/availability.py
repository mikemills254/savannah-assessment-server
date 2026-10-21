from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment, AppointmentStatus
from app.models.doctor_working_hours import DoctorWorkingHour

SLOT_DURATION = timedelta(minutes=30)
# A slot can be booked (or moved to) only this far ahead, so the doctor never
# finds a patient booked in with no warning.
MIN_BOOKING_NOTICE = timedelta(hours=1)


def slot_end(start: datetime) -> datetime:
    """30 minutes of real time after start, in start's timezone. Added in UTC:
    adding to a local datetime is wall-clock arithmetic, wrong across DST."""
    return (start.astimezone(UTC) + SLOT_DURATION).astimezone(start.tzinfo)


def local_slot_starts(
    day: date,
    periods: Iterable[tuple[time, time]],
    tz: ZoneInfo,
) -> list[datetime]:
    """Every 30-minute slot start on `day` within the working periods, as
    datetimes in the facility's timezone, earliest first."""
    starts: list[datetime] = []
    for start, end in sorted(periods):
        # Stepped in wall-clock time: a 09:00-12:00 period is 09:00, 09:30, ...
        # whatever the UTC offset that day.
        current = datetime.combine(day, start)
        period_end = datetime.combine(day, end)
        while current + SLOT_DURATION <= period_end:
            local = current.replace(tzinfo=tz)
            # Skips a wall time that doesn't exist that day (a DST gap).
            if local.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == current:
                starts.append(local)
            current += SLOT_DURATION
    return starts


def available_slot_starts(
    day: date,
    periods: Iterable[tuple[time, time]],
    tz: ZoneInfo,
    *,
    booked: Collection[datetime],
    now: datetime,
) -> list[datetime]:
    """Slot starts on `day` that are at least MIN_BOOKING_NOTICE away and not
    booked, i.e. the ones a booking would accept."""
    booked_utc = {b.astimezone(UTC) for b in booked}
    earliest = now + MIN_BOOKING_NOTICE
    return [
        start
        for start in local_slot_starts(day, periods, tz)
        if start >= earliest and start.astimezone(UTC) not in booked_utc
    ]


@dataclass(frozen=True)
class Shift:
    """One occurrence of a weekly working period, as real instants."""

    starts: datetime  # UTC
    ends: datetime  # UTC
    weekday: int
    start_time: time
    end_time: time

    def overlaps(self, other: "Shift") -> bool:
        return self.starts < other.ends and other.starts < self.ends


def shifts(
    periods: Iterable[tuple[int, time, time]],
    tz: ZoneInfo,
    first: date,
    days: int,
) -> list[Shift]:
    """Each weekly (weekday, start, end) period's occurrences on `days` local
    dates from `first`, as UTC instants. Facilities in different timezones
    can be compared this way, which weekday numbers alone can't do."""
    out: list[Shift] = []
    for i in range(days):
        day = first + timedelta(days=i)
        for weekday, start, end in periods:
            if weekday == day.weekday():
                out.append(
                    Shift(
                        datetime.combine(day, start, tzinfo=tz).astimezone(UTC),
                        datetime.combine(day, end, tzinfo=tz).astimezone(UTC),
                        weekday,
                        start,
                        end,
                    )
                )
    return out


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The UTC instants where `day` starts and ends in the timezone."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def first_slot(slots_by_day: Mapping[date, list[datetime]]) -> datetime | None:
    """The earliest slot start, given each day's starts in date order."""
    return next((starts[0] for starts in slots_by_day.values() if starts), None)


@dataclass(frozen=True)
class Schedules:
    """Doctors' weekly working periods and booked slot starts, loaded once so
    free slots can be worked out in memory for any of their days."""

    # (doctor id, facility id) -> weekday (Monday = 0) -> working periods
    periods: dict[tuple[UUID, UUID], dict[int, list[tuple[time, time]]]]
    # doctor id -> start times of active bookings, at any facility: a doctor
    # booked at one facility isn't free at another at the same time
    booked: dict[UUID, set[datetime]]

    def free_slots(
        self,
        doctor_id: UUID,
        facility_id: UUID,
        day: date,
        tz: ZoneInfo,
        *,
        now: datetime,
    ) -> list[datetime]:
        """The doctor's free slot starts at the facility on a local date
        there, as available_slot_starts works them out."""
        return available_slot_starts(
            day,
            self.periods.get((doctor_id, facility_id), {}).get(day.weekday(), []),
            tz,
            booked=self.booked.get(doctor_id, set()),
            now=now,
        )


class AvailabilityService:
    def __init__(self, db: Session):
        self.db = db

    def schedules(
        self, doctor_ids: Collection[UUID], start: datetime, end: datetime
    ) -> Schedules:
        """The doctors' working hours at every facility, and their active
        bookings starting in [start, end). Two queries however many doctors;
        cancelled bookings free their slot, so they're left out."""
        periods: dict[tuple[UUID, UUID], dict[int, list[tuple[time, time]]]] = {}
        booked: dict[UUID, set[datetime]] = {}
        if not doctor_ids:
            return Schedules(periods, booked)

        hours = self.db.execute(
            select(
                DoctorWorkingHour.doctor_id,
                DoctorWorkingHour.facility_id,
                DoctorWorkingHour.weekday,
                DoctorWorkingHour.start_time,
                DoctorWorkingHour.end_time,
            ).where(DoctorWorkingHour.doctor_id.in_(doctor_ids))
        )
        for doctor_id, facility_id, weekday, period_start, period_end in hours:
            periods.setdefault((doctor_id, facility_id), {}).setdefault(
                weekday, []
            ).append((period_start, period_end))

        bookings = self.db.execute(
            select(Appointment.doctor_id, Appointment.starts_at).where(
                Appointment.doctor_id.in_(doctor_ids),
                Appointment.status == AppointmentStatus.BOOKED,
                Appointment.starts_at >= start,
                Appointment.starts_at < end,
            )
        )
        for doctor_id, starts_at in bookings:
            booked.setdefault(doctor_id, set()).add(starts_at)

        return Schedules(periods, booked)
