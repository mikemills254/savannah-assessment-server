from datetime import datetime
from functools import cache
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import available_timezones

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
)

from app.core.permissions import UserRole
from app.schemas.user import PersonName


@cache
def _timezone_names() -> frozenset[str]:
    # Checked against the list rather than by loading the zone: a tzdata
    # folder name such as "America" can raise an OSError instead of
    # ZoneInfoNotFoundError.
    return frozenset(available_timezones())


def _known_timezone(value: str) -> str:
    if value not in _timezone_names():
        raise ValueError("Unknown timezone; use an IANA name such as Africa/Nairobi")
    return value


FacilityName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=150),
]

# IANA name, e.g. "Africa/Nairobi".
Timezone = Annotated[
    str,
    AfterValidator(_known_timezone),
    Field(examples=["Africa/Nairobi"]),
]


class FacilityCreate(BaseModel):
    name: FacilityName
    timezone: Timezone


class FacilityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    timezone: str
    is_active: bool
    created_at: datetime


StaffRole = Literal[UserRole.FACILITY_ADMIN, UserRole.DOCTOR]


class MemberCreate(BaseModel):
    email: EmailStr
    role: StaffRole
    # Required when the email has no account yet; ignored otherwise, so an
    # admin can't rename an existing user.
    name: PersonName | None = None


class MemberRead(BaseModel):
    """A facility admin or doctor. For a DOCTOR, id is the doctor_id used by
    working hours and appointments."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    facility_id: UUID
    name: str
    email: str
    role: StaffRole
    is_active: bool
    created_at: datetime


class InviteRead(BaseModel):
    # Send this to the person (e.g. as a link); they use it on
    # POST /auth/accept-invite to set their password.
    invite_token: str


class MemberCreated(BaseModel):
    member: MemberRead
    # Set when the person has never set a password (a new account, or one
    # invited earlier that hasn't been accepted). Null when they can already
    # log in.
    invite_token: str | None
