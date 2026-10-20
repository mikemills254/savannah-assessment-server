from datetime import datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.models.appointment import AppointmentStatus
from app.schemas.doctor import FacilitySummary
from app.schemas.patient import PatientRef


class AppointmentCreate(BaseModel):
    """The patient is the caller and the end time is set by the server, so
    neither is sent."""

    doctor_id: UUID
    # Must include an offset, e.g. 2026-10-20T09:00:00+03:00, and fall on the
    # hour or half hour in the facility's local time.
    starts_at: AwareDatetime = Field(examples=["2026-10-20T09:00:00+03:00"])


class StaffAppointmentCreate(AppointmentCreate):
    """A facility admin booking on a patient's behalf."""

    patient: PatientRef


class AppointmentCancel(BaseModel):
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
        Field(examples=["Feeling better, no longer need the visit"]),
    ]


class AppointmentReschedule(BaseModel):
    """The new start, with the same doctor. Same rules as starts_at when
    booking."""

    starts_at: AwareDatetime = Field(examples=["2026-10-21T14:30:00+03:00"])


class PersonSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str


class AppointmentRead(BaseModel):
    """Times are in the facility's local time."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: AppointmentStatus
    starts_at: datetime
    ends_at: datetime
    created_at: datetime
    cancelled_at: datetime | None
    doctor: PersonSummary
    patient: PersonSummary
    # The patient, or the facility admin who booked for them. Null for
    # bookings made before this was recorded.
    booked_by: PersonSummary | None
    # Set once cancelled: the patient, the doctor or a facility admin.
    cancelled_by: PersonSummary | None
    # Set once cancelled. Null for cancellations made before it was recorded.
    cancellation_reason: str | None
    facility: FacilitySummary

    @model_validator(mode="after")
    def _to_facility_time(self) -> "AppointmentRead":
        # Stored in UTC; shown the way the facility's working hours are.
        tz = ZoneInfo(self.facility.timezone)
        self.starts_at = self.starts_at.astimezone(tz)
        self.ends_at = self.ends_at.astimezone(tz)
        return self


class AppointmentBooked(BaseModel):
    appointment: AppointmentRead
    # Set when the patient has never signed in (e.g. their account was just
    # created for this booking). Send it to them; they use it on
    # POST /auth/accept-invite to set a password.
    invite_token: str | None
