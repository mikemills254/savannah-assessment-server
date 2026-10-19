from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

from app.schemas.user import PersonName


class PatientRef(BaseModel):
    """Who a staff booking is for. An existing patient account with this
    email is used (name is then ignored); otherwise one is created."""

    email: EmailStr
    # Required when the email has no account yet.
    name: PersonName | None = None


class PatientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    email: str
