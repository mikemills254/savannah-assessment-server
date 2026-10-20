from datetime import date, datetime, time
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


class WorkingHoursUpdate(BaseModel):
    """The doctor's whole weekly schedule; it replaces the current one. An
    empty list clears it."""

    periods: list[WorkingHourIn] = Field(max_length=100)

    @model_validator(mode="after")
    def _no_overlaps(self) -> "WorkingHoursUpdate":
        ordered = sorted(self.periods, key=lambda p: (p.weekday, p.start_time))
        for previous, current in zip(ordered, ordered[1:]):
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


class DoctorDetail(DoctorRead):
    facility: FacilitySummary
    working_hours: list[WorkingHourRead]


class SlotRead(BaseModel):
    # In the facility's local time.
    starts_at: datetime
    ends_at: datetime


class AvailabilityRead(BaseModel):
    doctor_id: UUID
    date: date
    timezone: str
    available_slots: list[SlotRead]
