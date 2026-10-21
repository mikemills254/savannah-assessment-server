from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.core.permissions import FacilityPermission
from app.dependencies.auth import FacilityAccess, require_facility_permission
from app.dependencies.db import DbSession
from app.models.user import User
from app.schemas.facility import InviteRead
from app.schemas.patient import PatientRead
from app.services.patients import PatientService

router = APIRouter(prefix="/facilities/{facility_id}/patients", tags=["patients"])

ManagePatients = Annotated[
    FacilityAccess,
    Depends(require_facility_permission(FacilityPermission.APPOINTMENTS_BOOK_ANY)),
]


@router.get("", response_model=list[PatientRead])
def list_patients(
    access: ManagePatients,
    db: DbSession,
    search: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[User]:
    """Facility admins: patients who have booked here, to pick when booking
    for a returning patient. search matches name or email."""
    return PatientService(db).list_for_facility(
        access.facility, search=search, limit=limit, offset=offset
    )


@router.post("/{patient_id}/invite", response_model=InviteRead)
def reissue_patient_invite(
    patient_id: UUID,
    access: ManagePatients,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> InviteRead:
    """New invite for a patient who has never signed in (e.g. the one from
    their booking expired). It's emailed to them too."""
    token = PatientService(db).reissue_invite(
        access.facility, patient_id, background_tasks=background_tasks
    )
    return InviteRead(invite_token=token)
