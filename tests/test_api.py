"""The HTTP contract of the booking endpoints: status codes, bodies, auth."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Appointment
from tests.conftest import auth


@pytest.fixture
def clinic(make):
    facility = make.facility()
    return facility, make.doctor(facility)


def book(client, patient, doctor, starts_at, **headers):
    return client.post(
        "/appointments",
        json={"doctor_id": str(doctor.id), "starts_at": starts_at.isoformat()},
        headers={**auth(patient), **headers},
    )


def free_slots(client, doctor, day) -> list[str]:
    response = client.get(
        f"/doctors/{doctor.id}/availability", params={"date": day.isoformat()}
    )
    assert response.status_code == 200, response.text
    return [slot["starts_at"][11:16] for slot in response.json()["available_slots"]]


class TestBook:
    def test_201_with_the_appointment_in_clinic_time(self, client, make, clinic, at):
        _, doctor = clinic

        response = book(client, make.patient(), doctor, at(9))

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "BOOKED"
        assert body["starts_at"] == at(9).isoformat()
        assert body["ends_at"] == at(9, 30).isoformat()
        assert body["doctor"]["id"] == str(doctor.id)
        assert body["cancellation_reason"] is None

    def test_401_without_a_token(self, client, clinic, at):
        _, doctor = clinic

        response = client.post(
            "/appointments",
            json={"doctor_id": str(doctor.id), "starts_at": at(9).isoformat()},
        )

        assert response.status_code == 401

    def test_409_when_the_slot_is_taken(self, client, make, clinic, at):
        _, doctor = clinic
        book(client, make.patient(), doctor, at(9))

        response = book(client, make.patient(), doctor, at(9))

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "This slot has just been booked by someone else; choose another"
        )

    def test_400_outside_working_hours(self, client, make, clinic, at):
        _, doctor = clinic

        response = book(client, make.patient(), doctor, at(19))

        assert response.status_code == 400
        assert response.json()["detail"] == "The doctor isn't working at this time"

    def test_422_without_a_utc_offset(self, client, make, clinic, at):
        _, doctor = clinic

        response = client.post(
            "/appointments",
            json={
                "doctor_id": str(doctor.id),
                "starts_at": at(9).replace(tzinfo=None).isoformat(),
            },
            headers=auth(make.patient()),
        )

        assert response.status_code == 422

    def test_a_retry_with_the_idempotency_key_replays(self, client, make, clinic, at):
        _, doctor = clinic
        patient = make.patient()

        first = book(client, patient, doctor, at(9), **{"Idempotency-Key": "abc"})
        retry = book(client, patient, doctor, at(9), **{"Idempotency-Key": "abc"})

        assert first.status_code == retry.status_code == 201
        assert retry.json()["id"] == first.json()["id"]
        assert "Idempotent-Replayed" not in first.headers
        assert retry.headers["Idempotent-Replayed"] == "true"


class TestAvailability:
    def test_lists_free_slots_and_hides_booked_ones(
        self, client, make, clinic, day, at
    ):
        _, doctor = clinic
        before = free_slots(client, doctor, day)
        book(client, make.patient(), doctor, at(9))

        after = free_slots(client, doctor, day)

        assert len(before) == 18  # 08:00-17:00 in 30-minute slots
        assert before[0] == "08:00" and before[-1] == "16:30"
        assert "09:00" in before and "09:00" not in after
        assert len(after) == 17

    def test_404_for_an_unknown_doctor(self, client, day):
        response = client.get(
            "/doctors/00000000-0000-0000-0000-000000000000/availability",
            params={"date": day.isoformat()},
        )

        assert response.status_code == 404

    def test_422_without_a_date(self, client, clinic):
        _, doctor = clinic

        assert client.get(f"/doctors/{doctor.id}/availability").status_code == 422


class TestCancel:
    def test_200_frees_the_slot(self, client, make, clinic, day, at):
        _, doctor = clinic
        patient = make.patient()
        appointment = book(client, patient, doctor, at(9)).json()

        response = client.patch(
            f"/appointments/{appointment['id']}/cancel",
            json={"reason": "  Feeling better  "},
            headers=auth(patient),
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "CANCELLED"
        assert response.json()["cancellation_reason"] == "Feeling better"
        assert "09:00" in free_slots(client, doctor, day)

    @pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}])
    def test_422_without_a_reason(self, client, make, clinic, at, body):
        _, doctor = clinic
        patient = make.patient()
        appointment = book(client, patient, doctor, at(9)).json()

        response = client.patch(
            f"/appointments/{appointment['id']}/cancel",
            json=body,
            headers=auth(patient),
        )

        assert response.status_code == 422

    def test_409_when_already_cancelled(self, client, make, clinic, at):
        _, doctor = clinic
        patient = make.patient()
        url = (
            f"/appointments/{book(client, patient, doctor, at(9)).json()['id']}/cancel"
        )
        client.patch(url, json={"reason": "first"}, headers=auth(patient))

        response = client.patch(url, json={"reason": "again"}, headers=auth(patient))

        assert response.status_code == 409
        assert response.json()["detail"] == "This appointment is already cancelled"

    def test_404_for_someone_elses_appointment(self, client, make, clinic, at):
        _, doctor = clinic
        appointment = book(client, make.patient(), doctor, at(9)).json()

        response = client.patch(
            f"/appointments/{appointment['id']}/cancel",
            json={"reason": "x"},
            headers=auth(make.patient()),
        )

        assert response.status_code == 404


class TestReschedule:
    def test_200_moves_it_and_frees_the_old_slot(self, client, make, clinic, day, at):
        _, doctor = clinic
        patient = make.patient()
        appointment = book(client, patient, doctor, at(9)).json()

        response = client.patch(
            f"/appointments/{appointment['id']}/reschedule",
            json={"starts_at": at(14, 30).isoformat()},
            headers=auth(patient),
        )

        assert response.status_code == 200, response.text
        assert response.json()["id"] == appointment["id"]
        assert response.json()["starts_at"] == at(14, 30).isoformat()
        slots = free_slots(client, doctor, day)
        assert "09:00" in slots and "14:30" not in slots

    def test_409_onto_a_taken_slot(self, client, make, clinic, at):
        _, doctor = clinic
        patient = make.patient()
        appointment = book(client, patient, doctor, at(9)).json()
        book(client, make.patient(), doctor, at(10))

        response = client.patch(
            f"/appointments/{appointment['id']}/reschedule",
            json={"starts_at": at(10).isoformat()},
            headers=auth(patient),
        )

        assert response.status_code == 409

    def test_409_when_cancelled(self, client, make, clinic, at):
        _, doctor = clinic
        patient = make.patient()
        appointment_id = book(client, patient, doctor, at(9)).json()["id"]
        client.patch(
            f"/appointments/{appointment_id}/cancel",
            json={"reason": "x"},
            headers=auth(patient),
        )

        response = client.patch(
            f"/appointments/{appointment_id}/reschedule",
            json={"starts_at": at(11).isoformat()},
            headers=auth(patient),
        )

        assert response.status_code == 409
        assert "cancelled" in response.json()["detail"]


class TestPatientAppointments:
    def upcoming(self, client, viewer, patient) -> list[dict]:
        response = client.get(
            f"/patients/{patient.id}/appointments", headers=auth(viewer)
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_the_patient_sees_upcoming_ones_soonest_first(
        self, client, make, clinic, at
    ):
        _, doctor = clinic
        patient = make.patient()
        later = book(client, patient, doctor, at(14)).json()
        sooner = book(client, patient, doctor, at(9)).json()
        cancelled = book(client, patient, doctor, at(11)).json()
        client.patch(
            f"/appointments/{cancelled['id']}/cancel",
            json={"reason": "Can't make it"},
            headers=auth(patient),
        )

        listed = self.upcoming(client, patient, patient)

        assert [a["id"] for a in listed] == [sooner["id"], later["id"]]

    def test_past_appointments_are_left_out(self, client, db, make, clinic):
        facility, doctor = clinic
        patient = make.patient()
        # Booking can't create a past one, so it's added directly.
        start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=2)
        db.add(
            Appointment(
                facility_id=facility.id,
                doctor_id=doctor.id,
                patient_id=patient.id,
                starts_at=start,
                ends_at=start + timedelta(minutes=30),
            )
        )
        db.commit()

        assert self.upcoming(client, patient, patient) == []

    def test_staff_see_only_what_they_could_see_anyway(self, client, make, clinic, at):
        facility, doctor = clinic
        other_doctor = make.doctor(facility)
        elsewhere = make.facility(name="Elsewhere")
        far_doctor = make.doctor(elsewhere)
        patient = make.patient()
        with_doctor = book(client, patient, doctor, at(9)).json()
        with_other = book(client, patient, other_doctor, at(10)).json()
        far_away = book(client, patient, far_doctor, at(11)).json()

        def ids(viewer) -> list[str]:
            return [a["id"] for a in self.upcoming(client, viewer, patient)]

        assert ids(make.admin(facility)) == [with_doctor["id"], with_other["id"]]
        assert ids(doctor) == [with_doctor["id"]]
        assert ids(make.admin(elsewhere)) == [far_away["id"]]

    def test_404_for_another_patient(self, client, make, clinic, at):
        _, doctor = clinic
        patient = make.patient()
        book(client, patient, doctor, at(9))

        response = client.get(
            f"/patients/{patient.id}/appointments", headers=auth(make.patient())
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "Patient not found"}

    def test_401_without_a_token(self, client, make):
        response = client.get(f"/patients/{make.patient().id}/appointments")

        assert response.status_code == 401
