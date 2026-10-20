from collections.abc import Collection, Iterable
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment, AppointmentStatus

SLOT_DURATION = timedelta(minutes=30)


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
    """Slot starts on `day` that are still in the future and not booked."""
    booked_utc = {b.astimezone(UTC) for b in booked}
    return [
        start
        for start in local_slot_starts(day, periods, tz)
        if start > now and start.astimezone(UTC) not in booked_utc
    ]


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The UTC instants where `day` starts and ends in the timezone."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


class AvailabilityService:
    def __init__(self, db: Session):
        self.db = db

    def booked_starts(self, doctor_id: UUID, day: date, tz: ZoneInfo) -> list[datetime]:
        """Start times of the doctor's active appointments on the local day."""
        day_start, day_end = local_day_bounds(day, tz)
        return list(
            self.db.scalars(
                select(Appointment.starts_at).where(
                    Appointment.doctor_id == doctor_id,
                    Appointment.status == AppointmentStatus.BOOKED,
                    Appointment.starts_at >= day_start,
                    Appointment.starts_at < day_end,
                )
            )
        )
