"""Booking rules at the service layer, against Postgres."""

from datetime import UTC, datetime, time, timedelta

import pytest

from app.core.errors import ForbiddenError, InvalidRequestError
from app.models import Appointment, AppointmentStatus
from app.services.appointments import (
    AppointmentService,
    IdempotencyKeyReusedError,
    PatientAlreadyBookedError,
    SlotTakenError,
)
from app.services.doctors import DoctorNotFoundError
from tests.conftest import TZ, next_slot_start


@pytest.fixture
def clinic(make):
    facility = make.facility()
    return facility, make.doctor(facility)


def test_books_a_free_slot(db, make, clinic, at):
    _, doctor = clinic
    patient = make.patient()

    appointment, replayed = AppointmentService(db).book(
        patient, doctor_id=doctor.id, starts_at=at(9)
    )

    assert not replayed
    assert appointment.status == AppointmentStatus.BOOKED
    assert appointment.starts_at == at(9)
    assert appointment.ends_at - appointment.starts_at == timedelta(minutes=30)
    assert appointment.patient_id == patient.id
    assert appointment.booked_by_id == patient.id


def test_accepts_the_same_instant_in_another_offset(db, make, clinic, at):
    _, doctor = clinic

    appointment, _ = AppointmentService(db).book(
        make.patient(), doctor_id=doctor.id, starts_at=at(9).astimezone(UTC)
    )

    assert appointment.starts_at == at(9)


@pytest.mark.parametrize(
    ("hour", "minute", "message"),
    [
        (7, 30, "isn't working"),  # before the 08:00 start
        (16, 30, None),  # last slot that fits before 17:00
        (17, 0, "isn't working"),  # would end after 17:00
        (9, 15, "half hour"),  # off the 30-minute grid
    ],
)
def test_slot_must_be_on_the_grid_within_working_hours(
    db, make, clinic, at, hour, minute, message
):
    _, doctor = clinic
    service = AppointmentService(db)

    if message is None:
        service.book(make.patient(), doctor_id=doctor.id, starts_at=at(hour, minute))
        return
    with pytest.raises(InvalidRequestError, match=message):
        service.book(make.patient(), doctor_id=doctor.id, starts_at=at(hour, minute))


def test_rejects_a_day_the_doctor_does_not_work(db, make, at):
    facility = make.facility()
    # Works every weekday except the test day's.
    other_days = [d for d in range(7) if d != at(9).weekday()]
    doctor = make.doctor(facility, weekdays=other_days)

    with pytest.raises(InvalidRequestError, match="isn't working"):
        AppointmentService(db).book(
            make.patient(), doctor_id=doctor.id, starts_at=at(9)
        )


def test_rejects_a_time_in_the_past(db, make, clinic):
    _, doctor = clinic
    yesterday_nine = datetime.combine(
        datetime.now(TZ).date() - timedelta(days=1), time(9), tzinfo=TZ
    )

    with pytest.raises(InvalidRequestError, match="in the past"):
        AppointmentService(db).book(
            make.patient(), doctor_id=doctor.id, starts_at=yesterday_nine
        )


def test_rejects_a_slot_less_than_an_hour_away(db, make):
    # Works around the clock, so only the notice rule can refuse it.
    doctor = make.doctor(make.facility(), hours=(time(0), time(23, 30)))

    with pytest.raises(InvalidRequestError, match="at least 1 hour ahead"):
        AppointmentService(db).book(
            make.patient(), doctor_id=doctor.id, starts_at=next_slot_start()
        )


def test_a_taken_slot_cannot_be_booked_by_someone_else(db, make, clinic, at):
    _, doctor = clinic
    service = AppointmentService(db)
    service.book(make.patient(), doctor_id=doctor.id, starts_at=at(9))

    with pytest.raises(SlotTakenError):
        service.book(make.patient(), doctor_id=doctor.id, starts_at=at(9))


def test_other_doctors_are_unaffected_by_a_booking(db, make, clinic, at):
    facility, doctor = clinic
    other = make.doctor(facility)
    service = AppointmentService(db)
    service.book(make.patient(), doctor_id=doctor.id, starts_at=at(9))

    appointment, _ = service.book(make.patient(), doctor_id=other.id, starts_at=at(9))

    assert appointment.doctor_id == other.id


def test_a_patient_cannot_hold_two_slots_at_once_in_one_facility(db, make, clinic, at):
    facility, doctor = clinic
    other = make.doctor(facility)
    patient = make.patient()
    service = AppointmentService(db)
    service.book(patient, doctor_id=doctor.id, starts_at=at(9))

    with pytest.raises(PatientAlreadyBookedError):
        service.book(patient, doctor_id=other.id, starts_at=at(9))


def test_a_cancelled_slot_can_be_booked_again(db, make, clinic, at):
    _, doctor = clinic
    first = make.patient()
    service = AppointmentService(db)
    appointment, _ = service.book(first, doctor_id=doctor.id, starts_at=at(9))
    service.cancel(first, appointment.id, reason="Can't make it")

    again, _ = service.book(make.patient(), doctor_id=doctor.id, starts_at=at(9))

    assert again.id != appointment.id
    assert again.status == AppointmentStatus.BOOKED


def test_only_patients_book_for_themselves(db, make, clinic, at):
    facility, doctor = clinic

    with pytest.raises(ForbiddenError):
        AppointmentService(db).book(
            make.admin(facility), doctor_id=doctor.id, starts_at=at(9)
        )


def test_a_deactivated_doctor_cannot_be_booked(db, make, clinic, at):
    _, doctor = clinic
    doctor.is_active = False
    db.commit()

    with pytest.raises(DoctorNotFoundError):
        AppointmentService(db).book(
            make.patient(), doctor_id=doctor.id, starts_at=at(9)
        )


def test_a_deactivated_facility_cannot_be_booked(db, make, clinic, at):
    facility, doctor = clinic
    facility.is_active = False
    db.commit()

    with pytest.raises(DoctorNotFoundError):
        AppointmentService(db).book(
            make.patient(), doctor_id=doctor.id, starts_at=at(9)
        )


class TestIdempotency:
    def test_a_retry_with_the_same_key_returns_the_original_booking(
        self, db, make, clinic, at
    ):
        _, doctor = clinic
        patient = make.patient()
        service = AppointmentService(db)

        first, first_replayed = service.book(
            patient, doctor_id=doctor.id, starts_at=at(9), idempotency_key="k-1"
        )
        second, second_replayed = service.book(
            patient, doctor_id=doctor.id, starts_at=at(9), idempotency_key="k-1"
        )

        assert (first_replayed, second_replayed) == (False, True)
        assert second.id == first.id
        assert db.query(Appointment).filter_by(patient_id=patient.id).count() == 1

    def test_reusing_a_key_for_a_different_booking_is_rejected(
        self, db, make, clinic, at
    ):
        _, doctor = clinic
        patient = make.patient()
        service = AppointmentService(db)
        service.book(
            patient, doctor_id=doctor.id, starts_at=at(9), idempotency_key="k-1"
        )

        with pytest.raises(IdempotencyKeyReusedError):
            service.book(
                patient, doctor_id=doctor.id, starts_at=at(10), idempotency_key="k-1"
            )

    def test_keys_are_per_user(self, db, make, clinic, at):
        _, doctor = clinic
        service = AppointmentService(db)
        service.book(
            make.patient(), doctor_id=doctor.id, starts_at=at(9), idempotency_key="k-1"
        )

        other, replayed = service.book(
            make.patient(), doctor_id=doctor.id, starts_at=at(10), idempotency_key="k-1"
        )

        assert not replayed
        assert other.starts_at == at(10)

    def test_a_failed_booking_does_not_burn_the_key(self, db, make, clinic, at):
        _, doctor = clinic
        patient = make.patient()
        service = AppointmentService(db)

        with pytest.raises(InvalidRequestError):
            service.book(
                patient, doctor_id=doctor.id, starts_at=at(7), idempotency_key="k-1"
            )
        # What the request's session does when it ends after an error.
        db.rollback()

        # The key is only stored with a booking, so the corrected retry
        # books instead of being refused as a different request.
        appointment, replayed = service.book(
            patient, doctor_id=doctor.id, starts_at=at(9), idempotency_key="k-1"
        )
        assert not replayed
        assert appointment.starts_at == at(9)
