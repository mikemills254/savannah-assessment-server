"""Doctors who work at several facilities: separate hours at each, one
diary across all of them, and each facility seeing only its own part."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.core.security import hash_password
from tests.conftest import auth

PASSWORD = "a-long-password"
HASHED = hash_password(PASSWORD)

MORNINGS = (time(8), time(12))
AFTERNOONS = (time(13), time(17))


@pytest.fixture
def two_clinics(make):
    """A doctor at Riverside in the mornings and Hillside in the afternoons
    (both in Nairobi time), with an admin at each."""
    riverside = make.facility(name="Riverside")
    hillside = make.facility(name="Hillside")
    doctor = make.doctor(riverside, hours=MORNINGS, hashed_password=HASHED)
    make.link(doctor, hillside, hours=AFTERNOONS)
    return (
        doctor,
        riverside,
        hillside,
        make.admin(riverside, hashed_password=HASHED),
        make.admin(hillside, hashed_password=HASHED),
    )


def book(client, patient, doctor, starts_at, **extra):
    return client.post(
        "/appointments",
        json={"doctor_id": str(doctor.id), "starts_at": starts_at.isoformat(), **extra},
        headers=auth(patient),
    )


def starts(slots) -> list[str]:
    return [s["starts_at"][11:16] for s in slots]


class TestSignIn:
    def test_a_doctor_lists_every_facility_they_work_at(self, client, two_clinics):
        doctor, riverside, hillside, *_ = two_clinics

        me = client.get("/auth/me", headers=auth(doctor)).json()

        assert [f["name"] for f in me["facilities"]] == ["Riverside", "Hillside"]
        assert me["facility_id"] is None


class TestBooking:
    def test_the_facility_is_worked_out_from_the_doctors_hours(
        self, client, make, at, two_clinics
    ):
        doctor, riverside, hillside, *_ = two_clinics

        morning = book(client, make.patient(), doctor, at(9))
        afternoon = book(client, make.patient(), doctor, at(14))

        assert morning.status_code == afternoon.status_code == 201
        assert morning.json()["facility"]["name"] == "Riverside"
        assert afternoon.json()["facility"]["name"] == "Hillside"

    def test_naming_a_facility_the_doctor_isnt_at_then_is_refused(
        self, client, make, at, two_clinics
    ):
        doctor, _, hillside, *_ = two_clinics

        response = book(
            client, make.patient(), doctor, at(9), facility_id=str(hillside.id)
        )

        assert response.status_code == 400
        assert "isn't working" in response.json()["detail"]

    def test_a_facility_they_dont_work_at_is_not_found(
        self, client, make, at, two_clinics
    ):
        doctor, *_ = two_clinics
        elsewhere = make.facility(name="Elsewhere")

        response = book(
            client, make.patient(), doctor, at(9), facility_id=str(elsewhere.id)
        )

        assert response.status_code == 404

    def test_one_booking_blocks_the_same_time_at_every_facility(self, client, make, at):
        # Overlapping hours can't be set through the API, but the database
        # still refuses a second booking for the doctor at the same time.
        riverside, hillside = make.facility(), make.facility()
        doctor = make.doctor(riverside)
        make.link(doctor, hillside)
        first = book(
            client, make.patient(), doctor, at(10), facility_id=str(riverside.id)
        )

        second = book(
            client, make.patient(), doctor, at(10), facility_id=str(hillside.id)
        )

        assert first.status_code == 201
        assert second.status_code == 409
        free_at_hillside = client.get(
            f"/doctors/{doctor.id}/availability",
            params={"date": at(10).date().isoformat(), "facility_id": str(hillside.id)},
        ).json()["available_slots"]
        assert "10:00" not in starts(free_at_hillside)


class TestAvailability:
    def test_covers_every_facility_and_names_each_slots_facility(
        self, client, day, two_clinics
    ):
        doctor, riverside, hillside, *_ = two_clinics

        body = client.get(
            f"/doctors/{doctor.id}/availability", params={"date": day.isoformat()}
        ).json()

        assert body["facility_id"] is None and body["timezone"] is None
        by_facility = {
            s["starts_at"][11:16]: s["facility_id"] for s in body["available_slots"]
        }
        assert by_facility["08:00"] == by_facility["11:30"] == str(riverside.id)
        assert by_facility["13:00"] == by_facility["16:30"] == str(hillside.id)
        assert "12:00" not in by_facility

    def test_one_facility_when_asked(self, client, day, two_clinics):
        doctor, _, hillside, *_ = two_clinics

        body = client.get(
            f"/doctors/{doctor.id}/availability",
            params={"date": day.isoformat(), "facility_id": str(hillside.id)},
        ).json()

        assert body["facility_id"] == str(hillside.id)
        assert body["timezone"] == "Africa/Nairobi"
        assert starts(body["available_slots"])[0] == "13:00"
        assert starts(body["available_slots"])[-1] == "16:30"

    def test_each_facilitys_page_uses_its_own_hours(self, client, day, two_clinics):
        doctor, riverside, hillside, *_ = two_clinics

        def first_free(facility) -> str:
            body = client.get(
                f"/facilities/{facility.id}/availability",
                params={"from": day.isoformat(), "days": 1},
            ).json()
            [entry] = body["doctors"]
            return starts(entry["days"][0]["available_slots"])[0]

        assert first_free(riverside) == "08:00"
        assert first_free(hillside) == "13:00"

    def test_the_doctor_lists_every_facility_with_its_hours(self, client, two_clinics):
        doctor, riverside, hillside, *_ = two_clinics

        body = client.get(f"/doctors/{doctor.id}").json()
        chosen = client.get(
            f"/doctors/{doctor.id}", params={"facility_id": str(hillside.id)}
        ).json()

        assert [f["facility"]["name"] for f in body["facilities"]] == [
            "Riverside",
            "Hillside",
        ]
        assert body["facility"]["name"] == "Riverside"
        assert chosen["facility"]["name"] == "Hillside"
        assert {h["start_time"] for h in chosen["working_hours"]} == {"13:00:00"}


class TestWorkingHours:
    def put(self, client, user, facility, doctor, periods):
        return client.put(
            f"/facilities/{facility.id}/doctors/{doctor.id}/working-hours",
            json={"periods": periods},
            headers=auth(user),
        )

    def test_hours_elsewhere_are_kept(self, client, two_clinics):
        doctor, riverside, hillside, admin, _ = two_clinics

        response = self.put(
            client,
            admin,
            riverside,
            doctor,
            [{"weekday": 0, "start_time": "09:00", "end_time": "11:00"}],
        )

        assert response.status_code == 200, response.text
        hillside_hours = client.get(
            f"/facilities/{hillside.id}/working-hours", headers=auth(doctor)
        ).json()
        assert len(hillside_hours) == 7
        assert {h["facility_id"] for h in hillside_hours} == {str(hillside.id)}

    def test_overlapping_another_facility_is_refused(self, client, two_clinics):
        doctor, riverside, _, admin, _ = two_clinics
        periods = [{"weekday": 2, "start_time": "11:00", "end_time": "14:00"}]

        as_admin = self.put(client, admin, riverside, doctor, periods)
        as_doctor = self.put(client, doctor, riverside, doctor, periods)

        assert as_admin.status_code == as_doctor.status_code == 409
        # Only the doctor is told where: Riverside's admin needn't know.
        assert "Hillside" not in as_admin.json()["detail"]
        assert as_doctor.json()["detail"] == (
            "Wednesday 11:00-14:00 overlaps your hours at Hillside"
        )

    def test_overlaps_are_checked_across_timezones(self, client, make):
        nairobi = make.facility(name="Nairobi", timezone="Africa/Nairobi")  # UTC+3
        lagos = make.facility(name="Lagos", timezone="Africa/Lagos")  # UTC+1
        doctor = make.doctor(nairobi, hours=(time(9), time(12)))
        make.link(doctor, lagos, hours=None)
        admin = make.admin(lagos)

        # 07:00-09:00 in Lagos is 09:00-11:00 in Nairobi.
        clash = self.put(
            client,
            admin,
            lagos,
            doctor,
            [{"weekday": 0, "start_time": "07:00", "end_time": "09:00"}],
        )
        # 06:00-07:00 in Lagos is 08:00-09:00 in Nairobi: just before.
        fine = self.put(
            client,
            admin,
            lagos,
            doctor,
            [{"weekday": 0, "start_time": "06:00", "end_time": "07:00"}],
        )

        assert clash.status_code == 409
        assert fine.status_code == 200, fine.text

    def test_a_facility_only_lists_its_own_hours(self, client, two_clinics):
        doctor, riverside, *_ = two_clinics

        hours = client.get(
            f"/facilities/{riverside.id}/working-hours", headers=auth(doctor)
        ).json()

        assert {h["start_time"] for h in hours} == {"08:00:00"}


class TestStaff:
    def add(self, client, admin, facility, email, role="DOCTOR"):
        return client.post(
            f"/facilities/{facility.id}/members",
            json={"email": email, "role": role},
            headers=auth(admin),
        )

    def test_adding_a_doctor_from_elsewhere_links_their_account(
        self, client, make, outbox
    ):
        riverside, hillside = (
            make.facility(name="Riverside"),
            make.facility(name="Hillside"),
        )
        doctor = make.doctor(riverside, hashed_password=HASHED)
        admin = make.admin(hillside)

        response = self.add(client, admin, hillside, doctor.email.upper())

        assert response.status_code == 201, response.text
        assert response.json()["invite_token"] is None
        assert response.json()["member"]["id"] == str(doctor.id)
        assert response.json()["member"]["is_active"] is True
        [email] = outbox
        assert email.template == "added_to_facility.html"
        assert f"/doctor/hours?facility={hillside.id}" in email.html
        facilities = client.get("/auth/me", headers=auth(doctor)).json()["facilities"]
        assert [f["name"] for f in facilities] == ["Riverside", "Hillside"]

    def test_adding_them_twice_is_a_conflict(self, client, two_clinics):
        doctor, _, hillside, _, hillside_admin = two_clinics

        assert (
            self.add(client, hillside_admin, hillside, doctor.email).status_code == 409
        )

    def test_a_patient_or_another_facilitys_admin_cant_be_added(self, client, make):
        facility = make.facility()
        admin = make.admin(facility)
        patient = make.patient()
        other_admin = make.admin(make.facility())

        for email in (patient.email, other_admin.email):
            assert self.add(client, admin, facility, email).status_code == 409
        assert (
            self.add(
                client, admin, facility, other_admin.email, "FACILITY_ADMIN"
            ).status_code
            == 409
        )

    def test_each_facility_lists_the_doctor_as_they_are_there(
        self, client, two_clinics
    ):
        doctor, riverside, hillside, riverside_admin, hillside_admin = two_clinics
        client.post(
            f"/facilities/{hillside.id}/members/{doctor.id}/deactivate",
            headers=auth(hillside_admin),
        )

        def listed(facility, admin) -> dict:
            members = client.get(
                f"/facilities/{facility.id}/members", headers=auth(admin)
            ).json()
            return next(m for m in members if m["id"] == str(doctor.id))

        assert listed(riverside, riverside_admin)["is_active"] is True
        assert listed(hillside, hillside_admin)["is_active"] is False
        assert listed(hillside, hillside_admin)["facility_id"] == str(hillside.id)

    def test_removing_them_from_one_facility_keeps_the_others(
        self, client, day, two_clinics
    ):
        doctor, riverside, hillside, riverside_admin, hillside_admin = two_clinics

        removed = client.post(
            f"/facilities/{hillside.id}/members/{doctor.id}/deactivate",
            headers=auth(hillside_admin),
        )

        assert removed.status_code == 200
        assert removed.json()["is_active"] is False
        login = client.post(
            "/auth/login", json={"email": doctor.email, "password": PASSWORD}
        )
        assert login.status_code == 200
        assert [f["name"] for f in login.json()["user"]["facilities"]] == ["Riverside"]
        assert client.get(f"/facilities/{hillside.id}/doctors").json() == []
        hillside_slots = client.get(
            f"/doctors/{doctor.id}/availability",
            params={"date": day.isoformat(), "facility_id": str(hillside.id)},
        )
        assert hillside_slots.status_code == 404

        # Removed from the last one too, they can no longer sign in.
        client.post(
            f"/facilities/{riverside.id}/members/{doctor.id}/deactivate",
            headers=auth(riverside_admin),
        )
        login = client.post(
            "/auth/login", json={"email": doctor.email, "password": PASSWORD}
        )
        assert login.status_code == 401

    def test_re_adding_them_restores_the_facility(self, client, two_clinics):
        doctor, _, hillside, _, hillside_admin = two_clinics
        client.post(
            f"/facilities/{hillside.id}/members/{doctor.id}/deactivate",
            headers=auth(hillside_admin),
        )

        response = self.add(client, hillside_admin, hillside, doctor.email)

        assert response.status_code == 201
        assert response.json()["member"]["is_active"] is True
        hours = client.get(
            f"/facilities/{hillside.id}/working-hours", headers=auth(hillside_admin)
        ).json()
        assert len(hours) == 7


class TestPrivacy:
    def test_each_facility_sees_only_its_own_appointments(
        self, client, make, at, two_clinics
    ):
        doctor, riverside, hillside, riverside_admin, hillside_admin = two_clinics
        morning = book(client, make.patient(), doctor, at(9)).json()
        afternoon = book(client, make.patient(), doctor, at(14)).json()

        def ids(facility, user) -> set[str]:
            listed = client.get(
                f"/facilities/{facility.id}/appointments", headers=auth(user)
            ).json()
            return {a["id"] for a in listed}

        assert ids(riverside, riverside_admin) == {morning["id"]}
        assert ids(hillside, hillside_admin) == {afternoon["id"]}
        # The doctor sees theirs at each facility.
        assert ids(riverside, doctor) == {morning["id"]}
        assert ids(hillside, doctor) == {afternoon["id"]}
        # Hillside's admin can't open or cancel Riverside's booking.
        assert (
            client.get(
                f"/appointments/{morning['id']}", headers=auth(hillside_admin)
            ).status_code
            == 404
        )

    def test_the_doctor_can_cancel_at_either_facility(
        self, client, make, at, two_clinics
    ):
        doctor, *_ = two_clinics
        afternoon = book(client, make.patient(), doctor, at(14)).json()

        response = client.patch(
            f"/appointments/{afternoon['id']}/cancel",
            json={"reason": "Called away"},
            headers=auth(doctor),
        )

        assert response.status_code == 200, response.text

    def test_a_doctors_other_facility_admin_cant_set_hours_here(
        self, client, two_clinics
    ):
        doctor, riverside, _, _, hillside_admin = two_clinics

        response = client.put(
            f"/facilities/{riverside.id}/doctors/{doctor.id}/working-hours",
            json={"periods": []},
            headers=auth(hillside_admin),
        )

        assert response.status_code == 403


class TestReschedule:
    def test_stays_at_the_same_facility(self, client, make, at, two_clinics):
        doctor, *_ = two_clinics
        patient = make.patient()
        booked = book(client, patient, doctor, at(9)).json()

        # 14:00 is when they're at Hillside, not Riverside.
        moved = client.patch(
            f"/appointments/{booked['id']}/reschedule",
            json={"starts_at": at(14).isoformat()},
            headers=auth(patient),
        )
        within = client.patch(
            f"/appointments/{booked['id']}/reschedule",
            json={"starts_at": at(10).isoformat()},
            headers=auth(patient),
        )

        assert moved.status_code == 400
        assert within.status_code == 200
        assert within.json()["facility"]["name"] == "Riverside"


def test_shifts_compare_real_instants():
    from app.services.availability import shifts

    monday = date(2026, 11, 2)
    nairobi = shifts([(0, time(9), time(12))], ZoneInfo("Africa/Nairobi"), monday, 1)
    lagos = shifts([(0, time(7), time(9))], ZoneInfo("Africa/Lagos"), monday, 1)

    [n], [lg] = nairobi, lagos
    assert n.starts == datetime(2026, 11, 2, 6, tzinfo=ZoneInfo("UTC"))
    assert lg.starts == datetime(2026, 11, 2, 6, tzinfo=ZoneInfo("UTC"))
    assert n.overlaps(lg)
    assert not n.overlaps(
        shifts([(0, time(6), time(7))], ZoneInfo("Africa/Lagos"), monday, 1)[0]
    )
