from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.schemas.facility import FacilityName, FacilityRead, Timezone
from app.schemas.user import Password, PersonName, UserRead


class RegisterRequest(BaseModel):
    """Public sign-up. Without a facility it creates a PATIENT.

    Sending facility_name as well creates that facility, with the new user
    as its FACILITY_ADMIN. timezone is then required; without a facility it
    is ignored."""

    name: PersonName
    email: EmailStr
    password: Password
    # The web client sends camelCase (facilityName); both spellings work.
    facility_name: FacilityName | None = Field(
        default=None,
        validation_alias=AliasChoices("facility_name", "facilityName"),
    )
    timezone: Timezone | None = None

    @field_validator("facility_name", "timezone", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        # A patient sign-up form may send empty strings for the facility fields.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="before")
    @classmethod
    def _ignore_timezone_without_facility(cls, data: Any) -> Any:
        # Drop the timezone before it's validated, so a patient's browser zone
        # can't fail a sign-up that has no facility.
        if isinstance(data, dict):
            facility = data.get("facility_name", data.get("facilityName"))
            if not (isinstance(facility, str) and facility.strip()):
                return {**data, "timezone": None}
        return data

    @model_validator(mode="after")
    def _facility_needs_timezone(self) -> "RegisterRequest":
        if self.facility_name is not None and self.timezone is None:
            raise PydanticCustomError(
                "timezone_required",
                "Choose your facility's time zone",
            )
        return self


class LoginRequest(BaseModel):
    email: EmailStr
    # Plain str: a wrong password should be a 401, not a validation error.
    password: str


class GoogleLoginRequest(BaseModel):
    # The "credential" from Google Identity Services on the client.
    id_token: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: Password


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeRead(UserRead):
    """The caller. The client picks the dashboard from role."""

    # The facility of a FACILITY_ADMIN or DOCTOR, null otherwise.
    facility: FacilityRead | None


class AuthResponse(TokenResponse):
    """A token plus who it belongs to, so the client needn't call /auth/me."""

    user: MeRead
