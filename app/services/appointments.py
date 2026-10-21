import hashlib
import logging
from datetime import UTC, date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    InvalidRequestError,
    NotFoundError,
)
from app.core.permissions import FacilityPermission, UserRole, facility_permissions
from app.models.appointment import Appointment, AppointmentStatus
from app.models.facility import Facility
from app.models.idempotent_key import IdempotencyKey
from app.models.user import User
from app.services.auth import AuthService
from app.services.availability import (
    MIN_BOOKING_NOTICE,
    local_day_bounds,
    local_slot_starts,
    slot_end,
)
from app.services.doctors import DoctorNotFoundError, DoctorService
from app.services.events import AppointmentEvent, publish_appointment_event
from app.services.patients import PatientService

logger = logging.getLogger(__name__)


class AppointmentNotFoundError(NotFoundError):
    detail = "Appointment not found"


class PatientNotFoundError(NotFoundError):
    detail = "Patient not found"


class SlotTakenError(ConflictError):
    detail = "This slot has just been booked by someone else; choose another"


class PatientAlreadyBookedError(ConflictError):
    detail = "The patient already has an appointment at this time at this facility"


class AlreadyCancelledError(ConflictError):
    detail = "This appointment is already cancelled"


class AlreadyStartedError(ConflictError):
    detail = "This appointment has already started and can't be cancelled"


class CancelledAppointmentError(ConflictError):
    detail = "This appointment is cancelled and can't be rescheduled; book a new one"


class DoctorUnavailableError(ConflictError):
    detail = "This doctor is no longer taking bookings; cancel and book another"


class IdempotencyKeyReusedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    detail = "This Idempotency-Key was already used for a different booking"


# Load what AppointmentRead shows in the same query.
_WITH_DETAILS = (
    joinedload(Appointment.doctor),
    joinedload(Appointment.patient),
    joinedload(Appointment.booked_by),
    joinedload(Appointment.cancelled_by),
    joinedload(Appointment.facility),
)


def _log_event(event: str, appointment: Appointment) -> None:
    # Ids only: who the patient is and why they cancelled stay in the db.
    logger.info(
        "Appointment %s",
        event.split(".")[1],
        extra={
            "event": event,
            "appointment_id": str(appointment.id),
            "facility_id": str(appointment.facility_id),
            "doctor_id": str(appointment.doctor_id),
            "starts_at": appointment.starts_at.isoformat(),
        },
    )


def _violated_constraint(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


class AppointmentService:
    def __init__(self, db: Session):
        self.db = db

    def book(
        self,
        patient: User,
        *,
        doctor_id: UUID,
        starts_at: datetime,
        facility_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[Appointment, bool]:
        """A patient booking a 30-minute slot for themselves. Returns the
        appointment and whether it is a replay of an earlier request with the
        same Idempotency-Key, in which case nothing new was booked.

        For a doctor at several facilities, the facility is the one whose
        working hours the slot falls in, unless facility_id names it."""
        if patient.role != UserRole.PATIENT:
            raise ForbiddenError("Only patients can book for themselves")

        starts_at = starts_at.astimezone(UTC)
        request = f"{doctor_id}|{starts_at.isoformat()}"
        if facility_id is not None:
            request += f"|{facility_id}"
        key_row, replay = self._claim_key(patient, idempotency_key, request)
        if replay is not None:
            _log_event("appointment.replayed", replay)
            return replay, True

        doctor, facilities = DoctorService(self.db).bookable_facilities(
            doctor_id, facility_id
        )
        facility = self._facility_at(doctor, facilities, starts_at)
        return self._insert(
            doctor, facility, patient, patient, starts_at, key_row
        ), False

    def _facility_at(
        self, doctor: User, facilities: list[Facility], starts_at: datetime
    ) -> Facility:
        """Of the doctor's facilities, the one they work at when the slot
        starts. Their hours at different facilities can't overlap, so there
        is at most one; with none, the first is returned and _check_slot
        then says what's wrong with the time."""
        if len(facilities) == 1:
            return facilities[0]
        doctors = DoctorService(self.db)
        working = []
        for facility in facilities:
            tz = ZoneInfo(facility.timezone)
            local = starts_at.astimezone(tz)
            periods = doctors.periods_on(doctor.id, facility.id, local.weekday())
            if local in local_slot_starts(local.date(), periods, tz):
                working.append(facility)
        if len(working) > 1:
            raise InvalidRequestError(
                "The doctor works at more than one facility at this time; "
                "send facility_id to choose"
            )
        return working[0] if working else facilities[0]

    def book_for_patient(
        self,
        facility: Facility,
        actor: User,
        *,
        doctor_id: UUID,
        starts_at: datetime,
        patient_email: str,
        patient_name: str | None,
        idempotency_key: str | None = None,
    ) -> tuple[Appointment, bool]:
        """A facility admin booking one of the facility's doctors for a
        patient, whose account is created if they don't have one. The route
        checks the admin's permission. Returns as book() does."""
        starts_at = starts_at.astimezone(UTC)
        email = AuthService.normalize_email(patient_email)
        key_row, replay = self._claim_key(
            actor,
            idempotency_key,
            f"{facility.id}|{doctor_id}|{starts_at.isoformat()}|{email}",
        )
        if replay is not None:
            _log_event("appointment.replayed", replay)
            return replay, True

        # Not found unless the doctor works here.
        doctor = DoctorService(self.db).get_bookable(doctor_id, facility)
        # A new patient's account is only kept if the booking succeeds.
        patient = PatientService(self.db).get_or_create(email=email, name=patient_name)
        return self._insert(doctor, facility, patient, actor, starts_at, key_row), False

    def _claim_key(
        self,
        actor: User,
        key: str | None,
        request: str,
    ) -> tuple[IdempotencyKey | None, Appointment | None]:
        """Claim the Idempotency-Key, if one was sent. Returns the claimed key,
        or the appointment an earlier request with it created.

        Claimed first, in the same transaction as the booking: a concurrent
        request with the same key waits on the unique index here, then
        replays this booking instead of making its own. If booking fails,
        the claim rolls back with it and a retry starts afresh."""
        if key is None:
            return None, None

        request_hash = hashlib.sha256(request.encode()).hexdigest()
        key_row = IdempotencyKey(user_id=actor.id, key=key, request_hash=request_hash)
        self.db.add(key_row)
        try:
            self.db.flush()
            return key_row, None
        except IntegrityError as exc:
            self.db.rollback()
            if _violated_constraint(exc) != "uq_idempotency_user_key":
                raise

        existing = self.db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == actor.id,
                IdempotencyKey.key == key,
            )
        )
        if existing is None or existing.appointment_id is None:
            # Not expected: the key and its appointment commit together.
            raise ConflictError("A request with this Idempotency-Key is in progress")
        if existing.request_hash != request_hash:
            raise IdempotencyKeyReusedError
        return None, self._get_with_details(existing.appointment_id)

    def _insert(
        self,
        doctor: User,
        facility: Facility,
        patient: User,
        booked_by: User,
        starts_at: datetime,
        key_row: IdempotencyKey | None,
    ) -> Appointment:
        """Validate the slot and save the booking."""
        self._check_slot(doctor, facility, starts_at)
        appointment = Appointment(
            facility_id=facility.id,
            doctor_id=doctor.id,
            patient_id=patient.id,
            booked_by_id=booked_by.id,
            starts_at=starts_at,
            ends_at=slot_end(starts_at),
        )
        self.db.add(appointment)
        self._flush_slot()

        if key_row is not None:
            key_row.appointment_id = appointment.id
        self.db.commit()
        _log_event("appointment.booked", appointment)
        booked = self._get_with_details(appointment.id)
        publish_appointment_event(AppointmentEvent.BOOKED, booked, booked_by)
        return booked

    def _flush_slot(self) -> None:
        """Flush a new or moved booking.

        The partial unique indexes are what stop concurrent requests double
        booking the doctor or the patient; the checks before them only give
        clearer errors for the common cases."""
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            constraint = _violated_constraint(exc)
            logger.info(
                "Booking conflict",
                extra={"event": "appointment.conflict", "constraint": constraint},
            )
            if constraint == "uq_active_doctor_slot":
                raise SlotTakenError from None
            if constraint == "uq_active_patient_slot":
                raise PatientAlreadyBookedError from None
            raise

    def _check_slot(
        self, doctor: User, facility: Facility, starts_at: datetime
    ) -> None:
        tz = ZoneInfo(facility.timezone)
        local = starts_at.astimezone(tz)

        if local.minute % 30 or local.second or local.microsecond:
            raise InvalidRequestError(
                "Appointments start on the hour or half hour in the facility's time"
            )
        now = datetime.now(UTC)
        if starts_at <= now:
            raise InvalidRequestError("This time is in the past")
        if starts_at < now + MIN_BOOKING_NOTICE:
            raise InvalidRequestError(
                "Appointments must be booked at least 1 hour ahead; choose a later time"
            )

        day = local.date()
        periods = DoctorService(self.db).periods_on(
            doctor.id, facility.id, day.weekday()
        )
        if local not in local_slot_starts(day, periods, tz):
            raise InvalidRequestError("The doctor isn't working at this time")

    def get(self, user: User, appointment_id: UUID) -> Appointment:
        """An appointment the caller may see. Others are not found, so their
        existence isn't revealed."""
        appointment = self.db.scalar(
            select(Appointment)
            .where(Appointment.id == appointment_id)
            .options(*_WITH_DETAILS)
        )
        if appointment is None or not self._can(user, appointment, view=True):
            raise AppointmentNotFoundError
        return appointment

    def cancel(self, user: User, appointment_id: UUID, *, reason: str) -> Appointment:
        """Cancel a booked appointment, which frees its slot. The record is
        kept, with who cancelled it and why. Cancelling twice is a 409, and
        nothing changes."""
        # Locked so a concurrent cancellation waits and then sees CANCELLED.
        appointment = self.db.get(Appointment, appointment_id, with_for_update=True)
        if appointment is None or not self._can(user, appointment, view=True):
            raise AppointmentNotFoundError
        if not self._can(user, appointment, view=False):
            raise ForbiddenError("You can't cancel this appointment")
        if appointment.status == AppointmentStatus.CANCELLED:
            raise AlreadyCancelledError

        now = datetime.now(UTC)
        if appointment.starts_at <= now:
            raise AlreadyStartedError

        appointment.status = AppointmentStatus.CANCELLED
        appointment.cancelled_at = now
        appointment.cancelled_by_id = user.id
        appointment.cancellation_reason = reason
        self.db.commit()
        _log_event("appointment.cancelled", appointment)
        cancelled = self._get_with_details(appointment.id)
        publish_appointment_event(AppointmentEvent.CANCELLED, cancelled, user)
        return cancelled

    def reschedule(
        self, user: User, appointment_id: UUID, *, starts_at: datetime
    ) -> Appointment:
        """Move a booked appointment to another slot with the same doctor.
        Allowed for whoever may cancel it. The new slot is validated as for a
        new booking, and the old one is free as soon as this commits, since
        it's the same row moved. Moving to the time it already has changes
        nothing, so a retry after a lost response is safe."""
        # Locked so a concurrent cancel or reschedule waits for this one.
        appointment = self.db.get(Appointment, appointment_id, with_for_update=True)
        if appointment is None or not self._can(user, appointment, view=True):
            raise AppointmentNotFoundError
        if not self._can(user, appointment, view=False):
            raise ForbiddenError("You can't reschedule this appointment")
        if appointment.status == AppointmentStatus.CANCELLED:
            raise CancelledAppointmentError
        if appointment.starts_at <= datetime.now(UTC):
            raise AlreadyStartedError(
                "This appointment has already started and can't be rescheduled"
            )

        starts_at = starts_at.astimezone(UTC)
        if starts_at == appointment.starts_at:
            return self._get_with_details(appointment.id)

        # Same doctor, same facility: they must still be bookable there.
        facility = appointment.facility
        try:
            doctor = DoctorService(self.db).get_bookable(
                appointment.doctor_id, facility
            )
        except DoctorNotFoundError:
            raise DoctorUnavailableError from None
        self._check_slot(doctor, facility, starts_at)

        appointment.starts_at = starts_at
        appointment.ends_at = slot_end(starts_at)
        self._flush_slot()
        self.db.commit()
        _log_event("appointment.rescheduled", appointment)
        moved = self._get_with_details(appointment.id)
        publish_appointment_event(AppointmentEvent.RESCHEDULED, moved, user)
        return moved

    def list_for_patient(
        self,
        patient: User,
        *,
        status: AppointmentStatus | None,
        limit: int,
        offset: int,
    ) -> list[Appointment]:
        """The patient's own appointments, latest first."""
        query = select(Appointment).where(Appointment.patient_id == patient.id)
        if status is not None:
            query = query.where(Appointment.status == status)
        return list(
            self.db.scalars(
                query.options(*_WITH_DETAILS)
                .order_by(Appointment.starts_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )

    def upcoming_for_patient(
        self,
        actor: User,
        patient_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> list[Appointment]:
        """A patient's booked appointments that haven't ended (one in
        progress included), soonest first. The patient sees all of theirs, a
        facility admin those at their facility, and a doctor those with them.
        Anyone else gets not found, so patients' ids can't be probed."""
        query = select(Appointment).where(
            Appointment.patient_id == patient_id,
            Appointment.status == AppointmentStatus.BOOKED,
            Appointment.ends_at > datetime.now(UTC),
        )
        if actor.id != patient_id:
            # Staff permissions only ever apply at their own facility.
            permissions = facility_permissions(actor.role, is_own_facility=True)
            if FacilityPermission.APPOINTMENTS_VIEW_ANY in permissions:
                query = query.where(Appointment.facility_id == actor.facility_id)
            elif FacilityPermission.APPOINTMENTS_VIEW_OWN in permissions:
                query = query.where(Appointment.doctor_id == actor.id)
            else:
                raise PatientNotFoundError
        return list(
            self.db.scalars(
                query.options(*_WITH_DETAILS)
                .order_by(Appointment.starts_at)
                .limit(limit)
                .offset(offset)
            )
        )

    def list_for_facility(
        self,
        facility: Facility,
        *,
        actor: User,
        permissions: frozenset[FacilityPermission],
        from_day: date | None,
        to_day: date | None,
        doctor_id: UUID | None,
        status: AppointmentStatus | None,
        limit: int,
        offset: int,
    ) -> list[Appointment]:
        """The facility's appointments, earliest first, optionally between two
        local dates (inclusive). Admins see all of them; a doctor only their
        own."""
        if FacilityPermission.APPOINTMENTS_VIEW_ANY not in permissions:
            if FacilityPermission.APPOINTMENTS_VIEW_OWN not in permissions:
                raise ForbiddenError
            if doctor_id is not None and doctor_id != actor.id:
                raise ForbiddenError("Doctors can only see their own appointments")
            doctor_id = actor.id

        query = select(Appointment).where(Appointment.facility_id == facility.id)
        tz = ZoneInfo(facility.timezone)
        if from_day is not None:
            query = query.where(
                Appointment.starts_at >= local_day_bounds(from_day, tz)[0]
            )
        if to_day is not None:
            query = query.where(Appointment.starts_at < local_day_bounds(to_day, tz)[1])
        if doctor_id is not None:
            query = query.where(Appointment.doctor_id == doctor_id)
        if status is not None:
            query = query.where(Appointment.status == status)
        return list(
            self.db.scalars(
                query.options(*_WITH_DETAILS)
                .order_by(Appointment.starts_at)
                .limit(limit)
                .offset(offset)
            )
        )

    @staticmethod
    def _can(user: User, appointment: Appointment, *, view: bool) -> bool:
        """Whether the user may view (view=True) or cancel (view=False) it:
        its patient, an admin of its facility, or its doctor."""
        if appointment.patient_id == user.id:
            return True
        permissions = facility_permissions(
            user.role,
            is_own_facility=user.works_at(appointment.facility_id),
        )
        if view:
            any_, own = (
                FacilityPermission.APPOINTMENTS_VIEW_ANY,
                FacilityPermission.APPOINTMENTS_VIEW_OWN,
            )
        else:
            any_, own = (
                FacilityPermission.APPOINTMENTS_CANCEL_ANY,
                FacilityPermission.APPOINTMENTS_CANCEL_OWN,
            )
        return any_ in permissions or (
            own in permissions and appointment.doctor_id == user.id
        )

    def _get_with_details(self, appointment_id: UUID) -> Appointment:
        return self.db.scalars(
            select(Appointment)
            .where(Appointment.id == appointment_id)
            .options(*_WITH_DETAILS)
            .execution_options(populate_existing=True)
        ).one()
