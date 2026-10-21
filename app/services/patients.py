from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, InvalidRequestError, NotFoundError
from app.core.permissions import UserRole
from app.core.security import create_invite_token
from app.models.appointment import Appointment
from app.models.facility import Facility
from app.models.user import User
from app.services.auth import AuthService
from app.services.email import send_invite


class PatientNotFoundError(NotFoundError):
    detail = "Patient not found"


class NotAPatientError(ConflictError):
    detail = "This email belongs to a staff account, not a patient"


class PatientDeactivatedError(ConflictError):
    detail = "This patient's account has been deactivated"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PatientService:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, *, email: str, name: str | None) -> User:
        """The patient account with this email, or a new one without a
        password that they claim with an invite or Google sign-in. Added to
        the session, not committed, so it's saved with the booking or not
        at all."""
        user = AuthService(self.db).get_user_by_email(email)
        if user is not None:
            if user.role != UserRole.PATIENT:
                raise NotAPatientError
            if not user.is_active:
                raise PatientDeactivatedError
            return user

        if name is None:
            raise InvalidRequestError(
                "The patient's name is required when they don't have an account"
            )
        user = User(
            email=AuthService.normalize_email(email),
            name=name,
            hashed_password=None,
            role=UserRole.PATIENT,
        )
        self.db.add(user)
        try:
            self.db.flush()
        except IntegrityError:
            # A concurrent request created an account with this email first.
            self.db.rollback()
            raise ConflictError(
                "This patient was just added by another request; try again"
            ) from None
        return user

    def list_for_facility(
        self,
        facility: Facility,
        *,
        search: str | None,
        limit: int,
        offset: int,
    ) -> list[User]:
        """Patients who have booked at the facility, cancelled or not."""
        query = select(User).where(
            User.role == UserRole.PATIENT,
            select(Appointment.id)
            .where(
                Appointment.patient_id == User.id,
                Appointment.facility_id == facility.id,
            )
            .exists(),
        )
        if search:
            pattern = f"%{_escape_like(search.strip())}%"
            query = query.where(
                or_(
                    User.name.ilike(pattern, escape="\\"),
                    User.email.ilike(pattern, escape="\\"),
                )
            )
        return list(
            self.db.scalars(query.order_by(User.name).limit(limit).offset(offset))
        )

    def reissue_invite(
        self,
        facility: Facility,
        patient_id: UUID,
        *,
        background_tasks: BackgroundTasks,
    ) -> str:
        """A new invite for one of the facility's patients who has never
        signed in, e.g. because the first one expired. It's emailed to them
        too."""
        patient = self.db.scalar(
            select(User).where(
                User.id == patient_id,
                User.role == UserRole.PATIENT,
                select(Appointment.id)
                .where(
                    Appointment.patient_id == User.id,
                    Appointment.facility_id == facility.id,
                )
                .exists(),
            )
        )
        if patient is None:
            raise PatientNotFoundError
        token = self.invite_token_for(patient)
        if token is None:
            raise ConflictError("This patient has already signed in")
        self.email_invite(background_tasks, patient, facility, token)
        return token

    @staticmethod
    def email_invite(
        background_tasks: BackgroundTasks,
        patient: User,
        facility: Facility,
        token: str,
        appointment: Appointment | None = None,
    ) -> None:
        """Email the patient their invite after the response is sent, with
        the visit the clinic booked for them, if any."""
        details = None
        if appointment is not None:
            start = appointment.starts_at.astimezone(ZoneInfo(facility.timezone))
            details = {
                "doctor": appointment.doctor.name,
                # e.g. "Tuesday 28 October 2026, 10:30"; %-d isn't portable.
                "when": f"{start:%A} {start.day} {start:%B %Y, %H:%M}",
            }
        background_tasks.add_task(
            send_invite,
            user_id=patient.id,
            email=patient.email,
            name=patient.name,
            role=patient.role,
            facility_name=facility.name,
            token=token,
            appointment=details,
        )

    @staticmethod
    def invite_token_for(patient: User) -> str | None:
        """An invite if the patient has never signed in with a password or
        Google, otherwise None."""
        if patient.hashed_password is not None or patient.google_sub is not None:
            return None
        return create_invite_token(patient.id)
