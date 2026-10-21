"""Cancelling and rescheduling at the service layer, against Postgres."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import InvalidRequestError
from app.models import Appointment, AppointmentStatus
from app.services.appointments import (
    AlreadyCancelledError,
    AlreadyStartedError,
    AppointmentNotFoundError,
    AppointmentService,
    CancelledAppointmentError,
    DoctorUnavailableError,
    PatientAlreadyBookedError,
    SlotTakenError,
)
from tests.conftest import next_slot_start


@pytest.fixture
def booked(db, make, at):
    """A facility, its doctor, and a patient booked at 09:00."""
    facility = make.facility()
    doctor = make.doctor(facility)
    patient = make.patient()
    appointment, _ = AppointmentService(db).book(
        patient, doctor_id=doctor.id, starts_at=at(9)
    )
    return facility, doctor, patient, appointment


def _started_appointment(db, facility, doctor, patient) -> Appointment:
    """A booking that began 10 minutes ago; booking can't create one."""
    start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=10)
    appointment = Appointment(
        facility_id=facility.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
    )
    db.add(appointment)
    db.commit()
    return appointment


class TestCancel:
    def test_records_who_cancelled_and_why(self, db, booked):
        _, _, patient, appointment = booked

        cancelled = AppointmentService(db).cancel(
            patient, appointment.id, reason="Feeling better"
        )

        assert cancelled.status == AppointmentStatus.CANCELLED
        assert cancelled.cancellation_reason == "Feeling better"
        assert cancelled.cancelled_by_id == patient.id
        assert cancelled.cancelled_at is not None

    def test_cancelling_twice_is_a_conflict(self, db, booked):
        _, _, patient, appointment = booked
        service = AppointmentService(db)
        service.cancel(patient, appointment.id, reason="first")

        with pytest.raises(AlreadyCancelledError):
            service.cancel(patient, appointment.id, reason="second")
        assert service.get(patient, appointment.id).cancellation_reason == "first"

    def test_a_started_appointment_cannot_be_cancelled(self, db, make, booked):
        facility, doctor, _, _ = booked
        patient = make.patient()
        started = _started_appointment(db, facility, doctor, patient)

        with pytest.raises(AlreadyStartedError):
            AppointmentService(db).cancel(patient, started.id, reason="late")

    def test_another_patient_cannot_see_it(self, db, make, booked):
        _, _, _, appointment = booked

        with pytest.raises(AppointmentNotFoundError):
            AppointmentService(db).cancel(make.patient(), appointment.id, reason="x")

    def test_its_doctor_and_the_facility_admin_can_cancel(self, db, make, at, booked):
        facility, doctor, _, appointment = booked
        service = AppointmentService(db)
        service.cancel(doctor, appointment.id, reason="Doctor is ill")

        other, _ = service.book(make.patient(), doctor_id=doctor.id, starts_at=at(10))
        cancelled = service.cancel(
            make.admin(facility), other.id, reason="Clinic closed"
        )

        assert cancelled.status == AppointmentStatus.CANCELLED

    def test_another_facilitys_admin_cannot_see_it(self, db, make, booked):
        _, _, _, appointment = booked
        outsider = make.admin(make.facility(name="Elsewhere"))

        with pytest.raises(AppointmentNotFoundError):
            AppointmentService(db).cancel(outsider, appointment.id, reason="x")


class TestReschedule:
    def test_moves_the_booking_and_frees_the_old_slot(self, db, make, at, booked):
        _, doctor, patient, appointment = booked
        service = AppointmentService(db)

        moved = service.reschedule(patient, appointment.id, starts_at=at(11))

        assert moved.id == appointment.id
        assert moved.starts_at == at(11)
        assert moved.ends_at == at(11, 30)
        assert moved.status == AppointmentStatus.BOOKED
        freed, _ = service.book(make.patient(), doctor_id=doctor.id, starts_at=at(9))
        assert freed.starts_at == at(9)

    def test_the_new_slot_is_validated_like_a_booking(self, db, at, booked):
        _, _, patient, appointment = booked
        service = AppointmentService(db)
        last_week = at(9) - timedelta(days=14)

        with pytest.raises(InvalidRequestError, match="half hour"):
            service.reschedule(patient, appointment.id, starts_at=at(11, 10))
        with pytest.raises(InvalidRequestError, match="isn't working"):
            service.reschedule(patient, appointment.id, starts_at=at(20))
        with pytest.raises(InvalidRequestError, match="in the past"):
            service.reschedule(patient, appointment.id, starts_at=last_week)
        with pytest.raises(InvalidRequestError, match="at least 1 hour ahead"):
            service.reschedule(patient, appointment.id, starts_at=next_slot_start())

    def test_cannot_move_onto_a_taken_slot(self, db, make, at, booked):
        _, doctor, patient, appointment = booked
        service = AppointmentService(db)
        service.book(make.patient(), doctor_id=doctor.id, starts_at=at(10))

        with pytest.raises(SlotTakenError):
            service.reschedule(patient, appointment.id, starts_at=at(10))
        assert service.get(patient, appointment.id).starts_at == at(9)

    def test_cannot_move_onto_the_patients_other_booking(self, db, make, at, booked):
        facility, _, patient, appointment = booked
        other_doctor = make.doctor(facility)
        service = AppointmentService(db)
        service.book(patient, doctor_id=other_doctor.id, starts_at=at(10))

        with pytest.raises(PatientAlreadyBookedError):
            service.reschedule(patient, appointment.id, starts_at=at(10))

    def test_the_current_time_is_a_no_op(self, db, at, booked):
        _, _, patient, appointment = booked

        same = AppointmentService(db).reschedule(
            patient, appointment.id, starts_at=at(9)
        )

        assert same.starts_at == at(9)

    def test_a_cancelled_appointment_cannot_be_moved(self, db, at, booked):
        _, _, patient, appointment = booked
        service = AppointmentService(db)
        service.cancel(patient, appointment.id, reason="x")

        with pytest.raises(CancelledAppointmentError):
            service.reschedule(patient, appointment.id, starts_at=at(11))

    def test_a_started_appointment_cannot_be_moved(self, db, make, at, booked):
        facility, doctor, _, _ = booked
        patient = make.patient()
        started = _started_appointment(db, facility, doctor, patient)

        with pytest.raises(AlreadyStartedError, match="rescheduled"):
            AppointmentService(db).reschedule(patient, started.id, starts_at=at(11))

    def test_not_if_the_doctor_was_deactivated(self, db, at, booked):
        _, doctor, patient, appointment = booked
        doctor.is_active = False
        db.commit()

        with pytest.raises(DoctorUnavailableError):
            AppointmentService(db).reschedule(patient, appointment.id, starts_at=at(11))

    def test_another_patient_cannot_see_it(self, db, make, at, booked):
        _, _, _, appointment = booked

        with pytest.raises(AppointmentNotFoundError):
            AppointmentService(db).reschedule(
                make.patient(), appointment.id, starts_at=at(11)
            )

    def test_another_doctor_at_the_facility_cannot_move_it(self, db, make, at, booked):
        facility, _, _, appointment = booked
        colleague = make.doctor(facility)

        # Doctors only see their own appointments, so it's not found.
        with pytest.raises(AppointmentNotFoundError):
            AppointmentService(db).reschedule(
                colleague, appointment.id, starts_at=at(11)
            )
