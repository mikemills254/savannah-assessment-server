from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.dependencies.auth import FacilityAccess, get_facility_access
from app.dependencies.db import DbSession
from app.models.doctor_working_hours import DoctorWorkingHour
from app.models.user import User
from app.schemas.doctor import (
    AvailabilityRead,
    DoctorDetail,
    DoctorRead,
    SlotRead,
    WorkingHourRead,
    WorkingHoursUpdate,
)
from app.services.availability import slot_end
from app.services.doctors import DoctorService

router = APIRouter(tags=["doctors"])


@router.get("/facilities/{facility_id}/doctors", response_model=list[DoctorRead])
def list_doctors(facility_id: UUID, db: DbSession) -> list[User]:
    """Public: the facility's active doctors."""
    return DoctorService(db).list_for_facility(facility_id)


@router.get("/doctors/{doctor_id}", response_model=DoctorDetail)
def get_doctor(doctor_id: UUID, db: DbSession) -> DoctorDetail:
    """Public: a bookable doctor, their facility and weekly working hours."""
    service = DoctorService(db)
    doctor = service.get_bookable(doctor_id)
    return DoctorDetail.model_validate(
        {
            "id": doctor.id,
            "name": doctor.name,
            "facility_id": doctor.facility_id,
            "facility": doctor.facility,
            "working_hours": service.working_hours(doctor.id),
        }
    )


@router.get("/doctors/{doctor_id}/availability", response_model=AvailabilityRead)
def get_availability(
    doctor_id: UUID,
    day: Annotated[date, Query(alias="date", description="Local date at the facility")],
    db: DbSession,
) -> AvailabilityRead:
    """Public: the doctor's free 30-minute slots on a date, in the facility's
    time. A snapshot; a slot can still be taken before you book it."""
    doctor, starts = DoctorService(db).availability(doctor_id, day)
    return AvailabilityRead(
        doctor_id=doctor.id,
        date=day,
        timezone=doctor.facility.timezone,
        available_slots=[
            SlotRead(starts_at=start, ends_at=slot_end(start)) for start in starts
        ],
    )


@router.get(
    "/facilities/{facility_id}/working-hours",
    response_model=list[WorkingHourRead],
)
def list_facility_working_hours(
    access: Annotated[FacilityAccess, Depends(get_facility_access)],
    db: DbSession,
) -> list[DoctorWorkingHour]:
    """Staff: every doctor's weekly hours at the facility, including
    deactivated doctors'."""
    return DoctorService(db).facility_working_hours(
        access.facility, permissions=access.permissions
    )


@router.put(
    "/facilities/{facility_id}/doctors/{doctor_id}/working-hours",
    response_model=list[WorkingHourRead],
)
def replace_working_hours(
    doctor_id: UUID,
    body: WorkingHoursUpdate,
    access: Annotated[FacilityAccess, Depends(get_facility_access)],
    db: DbSession,
) -> list[DoctorWorkingHour]:
    """Replace a doctor's weekly schedule, in the facility's local time.
    Facility admins can set any doctor's; a doctor only their own. Existing
    appointments are kept."""
    return DoctorService(db).replace_working_hours(
        access.facility,
        doctor_id,
        body.periods,
        actor=access.user,
        permissions=access.permissions,
    )
