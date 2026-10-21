from datetime import date, datetime, time
from itertools import pairwise
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FacilitySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    # IANA name; working hours and availability are in this local time.
    timezone: str


class DoctorRead(BaseModel):
    """A bookable doctor. Contact details are left out: this is public."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    # The facility they're listed or shown at. A doctor can work at several;
    # DoctorDetail.facilities lists them all.
    facility_id: UUID


Weekday = Annotated[int, Field(ge=0, le=6, description="Monday = 0, Sunday = 6")]


class WorkingHourBase(BaseModel):
    weekday: Weekday
    # Local time at the doctor's facility; end_time is exclusive.
    start_time: time
    end_time: time


class WorkingHourIn(WorkingHourBase):
    @field_validator("start_time", "end_time")
    @classmethod
    def _on_slot_boundary(cls, value: time) -> time:
        # Slots start on the hour or half hour, so periods must too.
        if value.minute % 30 or value.second or value.microsecond:
            raise ValueError("Must be on the hour or half hour, e.g. 09:00 or 13:30")
        return value.replace(tzinfo=None)

    @model_validator(mode="after")
    def _valid_period(self) -> "WorkingHourIn":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class WorkingHourRead(WorkingHourBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    doctor_id: UUID
    # The facility these hours are at, in its local time.
    facility_id: UUID


class WorkingHoursUpdate(BaseModel):
    """The doctor's whole weekly schedule at one facility; it replaces their
    current one there. An empty list clears it. Hours at other facilities
    are kept, and these mustn't overlap them."""

    periods: list[WorkingHourIn] = Field(max_length=100)

    @model_validator(mode="after")
    def _no_overlaps(self) -> "WorkingHoursUpdate":
        ordered = sorted(self.periods, key=lambda p: (p.weekday, p.start_time))
        for previous, current in pairwise(ordered):
            if (
                previous.weekday == current.weekday
                and current.start_time < previous.end_time
            ):
                raise ValueError(
                    f"Periods overlap on weekday {current.weekday}: "
                    f"{previous.start_time}-{previous.end_time} and "
                    f"{current.start_time}-{current.end_time}"
                )
        return self


class DoctorFacilityRead(BaseModel):
    """A facility the doctor can be booked at, and their hours there."""

    facility: FacilitySummary
    working_hours: list[WorkingHourRead]


class DoctorDetail(DoctorRead):
    """facility_id, facility and working_hours are for the facility asked
    for (or the doctor's first); facilities lists every facility they can
    be booked at, first one first."""

    facility: FacilitySummary
    working_hours: list[WorkingHourRead]
    facilities: list[DoctorFacilityRead]


class SlotRead(BaseModel):
    # In the facility's local time.
    starts_at: datetime
    ends_at: datetime


class DoctorSlotRead(SlotRead):
    # Where the slot is, for a doctor at several facilities.
    facility_id: UUID


class AvailabilityRead(BaseModel):
    doctor_id: UUID
    date: date
    # The facility, when one was asked for or the doctor works at only one.
    # For a doctor at several, null, and each slot names its facility.
    facility_id: UUID | None
    timezone: str | None
    # Earliest first. Each in its facility's local time.
    available_slots: list[DoctorSlotRead]


class NextSlotRead(BaseModel):
    doctor_id: UUID
    starts_at: datetime
    ends_at: datetime


class DoctorDayRead(BaseModel):
    date: date
    available_slots: list[SlotRead]


class DoctorAvailabilityRead(BaseModel):
    doctor_id: UUID
    name: str
    # The earliest free slot in the requested range.
    next_available: SlotRead | None
    # Every date in the range, in order.
    days: list[DoctorDayRead]


class DayCountRead(BaseModel):
    date: date
    # Free slots across all of the facility's doctors that day.
    open_count: int


class FacilityAvailabilityRead(BaseModel):
    facility_id: UUID
    timezone: str
    # "from" is a Python keyword; it's the name in the JSON.
    from_date: date = Field(alias="from")
    # The last date covered, inclusive.
    to: date
    # The earliest free slot in the range, with any doctor.
    next_available: NextSlotRead | None
    days: list[DayCountRead]
    # Bookable doctors by name, including those with no free slots.
    doctors: list[DoctorAvailabilityRead]
