from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.core.permissions import UserRole

PersonName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]

Password = Annotated[str, StringConstraints(min_length=8, max_length=128)]


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    name: str
    role: UserRole
    # A FACILITY_ADMIN's facility; null otherwise. A doctor's facilities are
    # in MeRead.facilities.
    facility_id: UUID | None
    is_active: bool
    created_at: datetime
