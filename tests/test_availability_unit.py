"""The slot arithmetic, without a database."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.services.availability import (
    available_slot_starts,
    local_day_bounds,
    local_slot_starts,
    slot_end,
)

NAIROBI = ZoneInfo("Africa/Nairobi")
LONDON = ZoneInfo("Europe/London")
DAY = date(2026, 11, 2)


def hhmm(starts: list[datetime]) -> list[str]:
    return [s.strftime("%H:%M") for s in starts]


def test_slots_are_every_30_minutes_within_the_period():
    starts = local_slot_starts(DAY, [(time(9), time(11))], NAIROBI)
    assert hhmm(starts) == ["09:00", "09:30", "10:00", "10:30"]
    assert all(s.tzinfo == NAIROBI for s in starts)


def test_a_slot_that_would_overrun_the_period_is_left_out():
    assert hhmm(local_slot_starts(DAY, [(time(9), time(10, 15))], NAIROBI)) == [
        "09:00",
        "09:30",
    ]


def test_several_periods_are_merged_in_order():
    periods = [(time(14), time(15)), (time(9), time(10))]
    assert hhmm(local_slot_starts(DAY, periods, NAIROBI)) == [
        "09:00",
        "09:30",
        "14:00",
        "14:30",
    ]


def test_no_working_hours_means_no_slots():
    assert local_slot_starts(DAY, [], NAIROBI) == []


def test_wall_times_skipped_by_spring_forward_are_not_offered():
    # UK clocks go from 01:00 to 02:00 on 29 March 2026.
    starts = local_slot_starts(date(2026, 3, 29), [(time(0), time(3))], LONDON)
    assert hhmm(starts) == ["00:00", "00:30", "02:00", "02:30"]


def test_slot_end_is_30_minutes_of_real_time_across_a_dst_change():
    start = datetime(2026, 3, 29, 0, 30, tzinfo=LONDON)
    end = slot_end(start)
    # In UTC: subtracting two datetimes with the same tzinfo compares wall times.
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(minutes=30)
    # 00:30 GMT + 30 minutes is 02:00 BST on the wall clock.
    assert end.astimezone(LONDON).strftime("%H:%M") == "02:00"


def test_available_slots_exclude_booked_ones_and_those_too_soon():
    periods = [(time(9), time(11))]
    booked = [datetime(2026, 11, 2, 7, 0, tzinfo=UTC)]  # 10:00 in Nairobi (UTC+3)
    now = datetime(2026, 11, 2, 8, 15, tzinfo=NAIROBI)
    free = available_slot_starts(DAY, periods, NAIROBI, booked=booked, now=now)
    # 09:00 is only 45 minutes away.
    assert hhmm(free) == ["09:30", "10:30"]


def test_a_slot_exactly_an_hour_away_is_available_but_not_one_sooner():
    periods = [(time(9), time(10))]

    def free(now: datetime) -> list[str]:
        return hhmm(available_slot_starts(DAY, periods, NAIROBI, booked=[], now=now))

    assert free(datetime(2026, 11, 2, 8, 0, tzinfo=NAIROBI)) == ["09:00", "09:30"]
    assert free(datetime(2026, 11, 2, 8, 1, tzinfo=NAIROBI)) == ["09:30"]


def test_local_day_bounds_are_the_utc_instants_of_local_midnight():
    start, end = local_day_bounds(DAY, NAIROBI)
    assert start == datetime(2026, 11, 1, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 2, 21, 0, tzinfo=UTC)
