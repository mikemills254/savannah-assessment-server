from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.core.permissions import FacilityPermission
from app.dependencies.auth import (
    CurrentUser,
    FacilityAccess,
    get_facility_access,
    require_facility_permission,
)
from app.dependencies.db import DbSession
from app.models.appointment import Appointment, AppointmentStatus
from app.schemas.appointment import (
    AppointmentBooked,
    AppointmentCancel,
    AppointmentCreate,
    AppointmentRead,
    AppointmentReschedule,
    StaffAppointmentCreate,
)
from app.services.appointments import AppointmentService
from app.services.patients import PatientService

router = APIRouter(tags=["appointments"])

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]

IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        min_length=1,
        max_length=255,
        description="A UUID the client generates per booking and reuses on retries",
    ),
]

BookForPatients = Annotated[
    FacilityAccess,
    Depends(require_facility_permission(FacilityPermission.APPOINTMENTS_BOOK_ANY)),
]


@router.post(
    "/appointments",
    response_model=AppointmentRead,
    status_code=status.HTTP_201_CREATED,
)
def book_appointment(
    body: AppointmentCreate,
    user: CurrentUser,
    db: DbSession,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Appointment:
    """Patients only: book a 30-minute slot for yourself. Facility admins
    book for patients with POST /facilities/{facility_id}/appointments.

    With an Idempotency-Key, a retry returns the original booking (marked by
    an Idempotent-Replayed: true header) instead of booking again. 409 means
    someone else took the slot."""
    appointment, replayed = AppointmentService(db).book(
        user,
        doctor_id=body.doctor_id,
        starts_at=body.starts_at,
        idempotency_key=idempotency_key,
    )
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return appointment


@router.get("/appointments/me", response_model=list[AppointmentRead])
def list_my_appointments(
    user: CurrentUser,
    db: DbSession,
    status: AppointmentStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[Appointment]:
    """Your appointments as a patient, latest first."""
    return AppointmentService(db).list_for_patient(
        user, status=status, limit=limit, offset=offset
    )


@router.get("/appointments/{appointment_id}", response_model=AppointmentRead)
def get_appointment(
    appointment_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> Appointment:
    """Its patient, its doctor, or an admin of its facility."""
    return AppointmentService(db).get(user, appointment_id)


@router.patch(
    "/appointments/{appointment_id}/cancel",
    response_model=AppointmentRead,
)
def cancel_appointment(
    appointment_id: UUID,
    body: AppointmentCancel,
    user: CurrentUser,
    db: DbSession,
) -> Appointment:
    """Its patient, its doctor, or an admin of its facility, giving a reason.
    The slot becomes free again. 409 if it's already cancelled or has
    started; 422 if the reason is missing or blank."""
    return AppointmentService(db).cancel(user, appointment_id, reason=body.reason)


@router.patch(
    "/appointments/{appointment_id}/reschedule",
    response_model=AppointmentRead,
)
def reschedule_appointment(
    appointment_id: UUID,
    body: AppointmentReschedule,
    user: CurrentUser,
    db: DbSession,
) -> Appointment:
    """Move to another slot with the same doctor; whoever may cancel it may
    move it. The old slot becomes free again. The new one is checked as for
    a booking: 400 if it's off the half-hour grid, in the past or outside
    working hours, 409 if it's taken. 409 if the appointment is cancelled or
    has started. Sending its current time changes nothing."""
    return AppointmentService(db).reschedule(
        user, appointment_id, starts_at=body.starts_at
    )


@router.post(
    "/facilities/{facility_id}/appointments",
    response_model=AppointmentBooked,
    status_code=status.HTTP_201_CREATED,
)
def book_for_patient(
    body: StaffAppointmentCreate,
    access: BookForPatients,
    db: DbSession,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> AppointmentBooked:
    """Facility admins: book one of the facility's doctors for a patient
    (phone, walk-in). A patient without an account gets one, and
    invite_token is returned to send them. Idempotency-Key works as for
    POST /appointments."""
    appointment, replayed = AppointmentService(db).book_for_patient(
        access.facility,
        access.user,
        doctor_id=body.doctor_id,
        starts_at=body.starts_at,
        patient_email=body.patient.email,
        patient_name=body.patient.name,
        idempotency_key=idempotency_key,
    )
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return AppointmentBooked(
        appointment=AppointmentRead.model_validate(appointment),
        invite_token=PatientService.invite_token_for(appointment.patient),
    )


@router.get(
    "/facilities/{facility_id}/appointments",
    response_model=list[AppointmentRead],
)
def list_facility_appointments(
    access: Annotated[FacilityAccess, Depends(get_facility_access)],
    db: DbSession,
    day: Annotated[
        date | None,
        Query(alias="date", description="One local date; same as from_date = to_date"),
    ] = None,
    from_date: Annotated[date | None, Query(description="Local date, inclusive")] = None,
    to_date: Annotated[date | None, Query(description="Local date, inclusive")] = None,
    doctor_id: UUID | None = None,
    status: AppointmentStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Offset = 0,
) -> list[Appointment]:
    """Staff: the facility's appointments, earliest first. Admins see all;
    a doctor only their own."""
    return AppointmentService(db).list_for_facility(
        access.facility,
        actor=access.user,
        permissions=access.permissions,
        from_day=from_date or day,
        to_day=to_date or day,
        doctor_id=doctor_id,
        status=status,
        limit=limit,
        offset=offset,
    )
