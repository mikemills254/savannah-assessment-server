from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.dependencies.auth import FacilityAccess, get_facility_access
from app.dependencies.db import DbSession
from app.models.doctor_working_hours import DoctorWorkingHour
from app.schemas.doctor import (
    AvailabilityRead,
    DayCountRead,
    DoctorAvailabilityRead,
    DoctorDayRead,
    DoctorDetail,
    DoctorRead,
    DoctorSlotRead,
    FacilityAvailabilityRead,
    NextSlotRead,
    SlotRead,
    WorkingHourRead,
    WorkingHoursUpdate,
)
from app.services.availability import first_slot, slot_end
from app.services.doctors import (
    MAX_AVAILABILITY_DAYS,
    DoctorNotFoundError,
    DoctorService,
)

router = APIRouter(tags=["doctors"])


def _slot(start: datetime) -> SlotRead:
    return SlotRead(starts_at=start, ends_at=slot_end(start))


FacilityFilter = Annotated[
    UUID | None,
    Query(description="For a doctor at several facilities: just this one"),
]


@router.get("/facilities/{facility_id}/doctors", response_model=list[DoctorRead])
def list_doctors(facility_id: UUID, db: DbSession) -> list[DoctorRead]:
    """Public: the facility's active doctors."""
    return [
        DoctorRead(id=doctor.id, name=doctor.name, facility_id=facility_id)
        for doctor in DoctorService(db).list_for_facility(facility_id)
    ]


@router.get("/doctors/{doctor_id}", response_model=DoctorDetail)
def get_doctor(
    doctor_id: UUID, db: DbSession, facility_id: FacilityFilter = None
) -> DoctorDetail:
    """Public: a bookable doctor, every facility they can be booked at, and
    their weekly working hours at each. facility, facility_id and
    working_hours are for `facility_id` if given, else their first."""
    service = DoctorService(db)
    doctor, facilities = service.bookable_facilities(doctor_id)
    at = [
        {"facility": f, "working_hours": service.working_hours(doctor.id, f.id)}
        for f in facilities
    ]
    shown = next((a for a in at if a["facility"].id == facility_id), None)
    if facility_id is not None and shown is None:
        raise DoctorNotFoundError
    shown = shown or at[0]
    return DoctorDetail.model_validate(
        {
            "id": doctor.id,
            "name": doctor.name,
            "facility_id": shown["facility"].id,
            "facility": shown["facility"],
            "working_hours": shown["working_hours"],
            "facilities": at,
        }
    )


@router.get("/doctors/{doctor_id}/availability", response_model=AvailabilityRead)
def get_availability(
    doctor_id: UUID,
    day: Annotated[date, Query(alias="date", description="Local date at the facility")],
    db: DbSession,
    facility_id: FacilityFilter = None,
) -> AvailabilityRead:
    """Public: the doctor's free 30-minute slots on a date, in the facility's
    time. For a doctor at several facilities, slots at all of them (each
    naming its facility), unless facility_id picks one. A booking at one
    facility blocks the same time at the others. A snapshot; a slot can
    still be taken before you book it."""
    result = DoctorService(db).availability(doctor_id, day, facility_id=facility_id)
    only = result.by_facility[0][0] if len(result.by_facility) == 1 else None
    slots = sorted(
        (
            DoctorSlotRead(starts_at=start, ends_at=slot_end(start), facility_id=f.id)
            for f, starts in result.by_facility
            for start in starts
        ),
        key=lambda slot: slot.starts_at,
    )
    return AvailabilityRead(
        doctor_id=result.doctor.id,
        date=day,
        facility_id=only.id if only else None,
        timezone=only.timezone if only else None,
        available_slots=slots,
    )


@router.get(
    "/facilities/{facility_id}/availability",
    response_model=FacilityAvailabilityRead,
)
def get_facility_availability(
    facility_id: UUID,
    db: DbSession,
    from_date: Annotated[
        date | None,
        Query(
            alias="from",
            description="First local date at the facility; defaults to today there",
        ),
    ] = None,
    days: Annotated[
        int, Query(description=f"How many days, 1 to {MAX_AVAILABILITY_DAYS}")
    ] = 14,
) -> FacilityAvailabilityRead:
    """Public: every bookable doctor's free 30-minute slots over several
    days, in the facility's time, with daily totals and the next free slot.
    Each doctor's slots on a date are the same as GET
    /doctors/{doctor_id}/availability returns. A snapshot; a slot can still
    be taken before you book it."""
    result = DoctorService(db).facility_availability(
        facility_id, first=from_date, days=days
    )

    doctors: list[DoctorAvailabilityRead] = []
    next_available: NextSlotRead | None = None
    for doctor, slots_by_day in result.doctors:
        first = first_slot(slots_by_day)
        if first is not None and (
            next_available is None or first < next_available.starts_at
        ):
            next_available = NextSlotRead(
                doctor_id=doctor.id, starts_at=first, ends_at=slot_end(first)
            )
        doctors.append(
            DoctorAvailabilityRead(
                doctor_id=doctor.id,
                name=doctor.name,
                next_available=_slot(first) if first is not None else None,
                days=[
                    DoctorDayRead(date=day, available_slots=[_slot(s) for s in starts])
                    for day, starts in slots_by_day.items()
                ],
            )
        )

    return FacilityAvailabilityRead.model_validate(
        {
            "facility_id": result.facility.id,
            "timezone": result.facility.timezone,
            "from": result.dates[0],
            "to": result.dates[-1],
            "next_available": next_available,
            "days": [
                DayCountRead(
                    date=day,
                    open_count=sum(len(slots[day]) for _, slots in result.doctors),
                )
                for day in result.dates
            ],
            "doctors": doctors,
        }
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
    """Replace a doctor's weekly schedule at this facility, in its local
    time. Facility admins can set any doctor's; a doctor only their own.
    Existing appointments are kept. 409 if the hours overlap the doctor's
    hours at another facility."""
    return DoctorService(db).replace_working_hours(
        access.facility,
        doctor_id,
        body.periods,
        actor=access.user,
        permissions=access.permissions,
    )
