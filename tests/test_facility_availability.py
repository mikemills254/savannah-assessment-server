"""GET /facilities/{id}/availability, and next_available on GET /facilities."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services import doctors as doctor_service
from tests.conftest import auth

KIRITIMATI = ZoneInfo("Pacific/Kiritimati")  # UTC+14: its date is ahead of UTC's


@pytest.fixture
def freeze(monkeypatch):
    """freeze(instant): availability is worked out as if it were `instant`."""

    def _freeze(instant: datetime) -> None:
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz)

        monkeypatch.setattr(doctor_service, "datetime", Frozen)

    return _freeze


def get(client, facility, **params):
    if isinstance(params.get("from_"), date):
        params["from"] = params.pop("from_").isoformat()
    return client.get(f"/facilities/{facility.id}/availability", params=params)


def ok(client, facility, **params) -> dict:
    response = get(client, facility, **params)
    assert response.status_code == 200, response.text
    return response.json()


def hhmm(slots: list[dict]) -> list[str]:
    return [s["starts_at"][11:16] for s in slots]


def doctor_entry(body: dict, doctor) -> dict:
    [entry] = [d for d in body["doctors"] if d["doctor_id"] == str(doctor.id)]
    return entry


def book(client, doctor, starts_at: datetime, patient) -> dict:
    response = client.post(
        "/appointments",
        json={"doctor_id": str(doctor.id), "starts_at": starts_at.isoformat()},
        headers=auth(patient),
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestShape:
    def test_one_entry_per_day_including_empty_ones(self, client, make, day):
        facility = make.facility()
        doctor = make.doctor(facility, weekdays=[day.weekday()], name="Dr. Only")

        body = ok(client, facility, from_=day, days=7)

        dates = [(day + timedelta(days=i)).isoformat() for i in range(7)]
        assert set(body) == {
            "facility_id",
            "timezone",
            "from",
            "to",
            "next_available",
            "days",
            "doctors",
        }
        assert body["facility_id"] == str(facility.id)
        assert body["timezone"] == "Africa/Nairobi"
        assert (body["from"], body["to"]) == (dates[0], dates[-1])
        assert body["days"] == [
            {"date": d, "open_count": 18 if i == 0 else 0} for i, d in enumerate(dates)
        ]
        [entry] = body["doctors"]
        assert set(entry) == {"doctor_id", "name", "next_available", "days"}
        assert (entry["doctor_id"], entry["name"]) == (str(doctor.id), "Dr. Only")
        assert [d["date"] for d in entry["days"]] == dates
        assert len(entry["days"][0]["available_slots"]) == 18
        assert all(d["available_slots"] == [] for d in entry["days"][1:])
        assert entry["days"][0]["available_slots"][0] == {
            "starts_at": f"{dates[0]}T08:00:00+03:00",
            "ends_at": f"{dates[0]}T08:30:00+03:00",
        }

    def test_defaults_to_14_days_from_today_at_the_facility(
        self, client, make, day, at, freeze
    ):
        freeze(at(10, 10))
        facility = make.facility()

        body = ok(client, facility)

        assert body["from"] == day.isoformat()
        assert body["to"] == (day + timedelta(days=13)).isoformat()
        assert len(body["days"]) == 14

    def test_working_hours_weekdays_count_from_monday_0(self, client, make, day):
        facility = make.facility()
        doctor = make.doctor(facility, weekdays=[0])

        entry = doctor_entry(ok(client, facility, from_=day, days=14), doctor)

        working = [d["date"] for d in entry["days"] if d["available_slots"]]
        assert len(working) == 2
        assert all(date.fromisoformat(d).strftime("%A") == "Monday" for d in working)


class TestSlots:
    def test_match_the_per_doctor_endpoint_for_every_doctor_and_date(
        self, client, make, day, at, freeze
    ):
        # Part way through the first day, so past slots are dropped too.
        freeze(at(10, 10))
        facility = make.facility()
        doctors = [
            make.doctor(facility, name="Dr. A"),
            make.doctor(facility, hours=(time(13), time(18)), name="Dr. B"),
            make.doctor(facility, weekdays=[day.weekday()], name="Dr. C"),
        ]
        book(client, doctors[0], at(11), make.patient())
        book(client, doctors[1], at(14, 30), make.patient())

        body = ok(client, facility, from_=day, days=3)

        for doctor in doctors:
            for entry_day in doctor_entry(body, doctor)["days"]:
                single = client.get(
                    f"/doctors/{doctor.id}/availability",
                    params={"date": entry_day["date"]},
                ).json()
                # The per-doctor slots also name their facility.
                assert {s.pop("facility_id") for s in single["available_slots"]} <= {
                    str(facility.id)
                }
                assert entry_day["available_slots"] == single["available_slots"]

    def test_booked_slots_are_left_out_and_cancelled_ones_come_back(
        self, client, make, day, at
    ):
        facility = make.facility()
        doctor = make.doctor(facility)
        patient = make.patient()
        book(client, doctor, at(9), make.patient())
        cancelled = book(client, doctor, at(9, 30), patient)
        response = client.patch(
            f"/appointments/{cancelled['id']}/cancel",
            json={"reason": "Feeling better"},
            headers=auth(patient),
        )
        assert response.status_code == 200, response.text

        body = ok(client, facility, from_=day, days=1)

        slots = hhmm(doctor_entry(body, doctor)["days"][0]["available_slots"])
        assert "09:00" not in slots
        assert "09:30" in slots
        assert body["days"][0]["open_count"] == 17

    def test_slots_under_an_hour_away_are_left_out_using_clinic_time(
        self, client, make, day, freeze
    ):
        # 10:00 on `day` in Kiritimati is 20:00 the day before in UTC.
        now = datetime.combine(day, time(10), tzinfo=KIRITIMATI)
        freeze(now)
        facility = make.facility(timezone="Pacific/Kiritimati")
        doctor = make.doctor(facility)

        body = ok(client, facility, days=2)

        assert body["from"] == day.isoformat()
        today, tomorrow = doctor_entry(body, doctor)["days"]
        # 10:00 and 10:30 are too soon; 11:00 is exactly an hour away.
        assert hhmm(today["available_slots"])[0] == "11:00"
        assert len(today["available_slots"]) == 12
        assert len(tomorrow["available_slots"]) == 18
        assert today["available_slots"][0]["starts_at"].endswith("+14:00")

    def test_from_can_be_a_later_date(self, client, make, day):
        facility = make.facility()

        body = ok(client, facility, from_=day + timedelta(days=3), days=2)

        assert body["from"] == (day + timedelta(days=3)).isoformat()
        assert body["to"] == (day + timedelta(days=4)).isoformat()


class TestNextAvailable:
    def test_earliest_free_slot_per_doctor_and_for_the_facility(
        self, client, make, day, at
    ):
        facility = make.facility()
        afternoons = make.doctor(facility, hours=(time(13), time(17)), name="Dr. A")
        mornings_on_day_two = make.doctor(
            facility,
            hours=(time(9), time(12)),
            weekdays=[(day + timedelta(days=1)).weekday()],
            name="Dr. B",
        )
        book(client, afternoons, at(13), make.patient())

        body = ok(client, facility, from_=day, days=3)

        assert doctor_entry(body, afternoons)["next_available"] == {
            "starts_at": at(13, 30).isoformat(),
            "ends_at": at(14).isoformat(),
        }
        tomorrow_9 = at(9) + timedelta(days=1)
        assert doctor_entry(body, mornings_on_day_two)["next_available"] == {
            "starts_at": tomorrow_9.isoformat(),
            "ends_at": (tomorrow_9 + timedelta(minutes=30)).isoformat(),
        }
        assert body["next_available"] == {
            "doctor_id": str(afternoons.id),
            "starts_at": at(13, 30).isoformat(),
            "ends_at": at(14).isoformat(),
        }

    def test_the_facility_takes_the_earliest_across_doctors(
        self, client, make, day, at
    ):
        facility = make.facility()
        make.doctor(facility, hours=(time(13), time(17)), name="Dr. A")
        early = make.doctor(facility, hours=(time(8), time(9)), name="Dr. Z")

        body = ok(client, facility, from_=day, days=1)

        assert body["next_available"]["doctor_id"] == str(early.id)
        assert body["next_available"]["starts_at"] == at(8).isoformat()

    def test_null_when_nothing_is_free_in_the_range(self, client, make, day):
        facility = make.facility()
        doctor = make.doctor(facility, weekdays=[(day + timedelta(days=1)).weekday()])

        body = ok(client, facility, from_=day, days=1)

        assert doctor_entry(body, doctor)["next_available"] is None
        assert body["next_available"] is None

    def test_null_for_a_facility_without_doctors(self, client, make, day):
        body = ok(client, make.facility(), from_=day, days=3)

        assert body["doctors"] == []
        assert body["next_available"] is None
        assert [d["open_count"] for d in body["days"]] == [0, 0, 0]


class TestDoctors:
    def test_only_active_doctors_by_name_including_one_without_hours(
        self, client, make, day
    ):
        facility = make.facility()
        make.doctor(facility, name="Dr. Gone", is_active=False)
        make.doctor(make.facility(name="Elsewhere"), name="Dr. Other Clinic")
        zara = make.doctor(facility, name="Dr. Zara")
        no_hours = make.doctor(facility, weekdays=range(0), name="Dr. Adams")

        body = ok(client, facility, from_=day, days=2)

        assert [d["name"] for d in body["doctors"]] == ["Dr. Adams", "Dr. Zara"]
        entry = doctor_entry(body, no_hours)
        assert entry["next_available"] is None
        assert entry["days"] == [
            {"date": day.isoformat(), "available_slots": []},
            {"date": (day + timedelta(days=1)).isoformat(), "available_slots": []},
        ]
        assert doctor_entry(body, zara)["next_available"] is not None


class TestErrors:
    def test_404_for_an_unknown_facility(self, client):
        response = client.get(
            "/facilities/00000000-0000-0000-0000-000000000000/availability"
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "Facility not found"}

    def test_404_for_an_inactive_facility(self, client, make):
        response = get(client, make.facility(is_active=False))

        assert response.status_code == 404

    @pytest.mark.parametrize("days", [0, -1, 32, 100])
    def test_400_for_days_out_of_range(self, client, make, days):
        response = get(client, make.facility(), days=days)

        assert response.status_code == 400
        assert response.json() == {"detail": "days must be between 1 and 31"}

    def test_1_and_31_days_are_allowed(self, client, make, day):
        facility = make.facility()

        assert len(ok(client, facility, from_=day, days=1)["days"]) == 1
        assert len(ok(client, facility, from_=day, days=31)["days"]) == 31

    def test_400_for_a_date_before_today_at_the_facility(
        self, client, make, day, freeze
    ):
        # Still `day` in Kiritimati, when it's already the day before in UTC.
        freeze(datetime.combine(day, time(1), tzinfo=KIRITIMATI))
        facility = make.facility(timezone="Pacific/Kiritimati")

        response = get(client, facility, from_=day - timedelta(days=1))

        assert response.status_code == 400
        assert "before today" in response.json()["detail"]

    @pytest.mark.parametrize("params", [{"from": "21-10-2026"}, {"days": "two"}])
    def test_422_for_values_that_arent_a_date_or_a_number(self, client, make, params):
        # Like every other endpoint's malformed query parameters.
        response = client.get(
            f"/facilities/{make.facility().id}/availability", params=params
        )

        assert response.status_code == 422


class TestQueries:
    def test_count_does_not_grow_with_doctors_or_bookings(
        self, client, db, make, day, at, count_queries
    ):
        small = make.facility(name="Small")
        make.doctor(small)
        large = make.facility(name="Large")
        large_doctors = [make.doctor(large, name=f"Dr. {i}") for i in range(6)]
        for i, doctor in enumerate(large_doctors):
            book(client, doctor, at(9 + i), make.patient())
        # As in a real request's new session, nothing is already loaded.
        db.expunge_all()

        with count_queries() as small_queries:
            ok(client, small, from_=day, days=14)
        with count_queries() as large_queries:
            ok(client, large, from_=day, days=14)

        assert len(large_queries) == len(small_queries)
        # Facility, doctors, working hours, bookings.
        assert len(large_queries) == 4


class TestFacilityList:
    def test_each_facility_has_its_next_free_slot(self, client, make, day, at, freeze):
        freeze(at(10, 10))
        open_now = make.facility(name="A Open")
        doctor = make.doctor(open_now)
        book(client, doctor, at(10, 30), make.patient())
        make.facility(name="B No Doctors")

        facilities = {f["name"]: f for f in client.get("/facilities").json()}

        # 10:30 is booked and 11:00 is under an hour away.
        assert facilities["A Open"]["next_available"] == {
            "doctor_id": str(doctor.id),
            "starts_at": at(11, 30).isoformat(),
            "ends_at": at(12).isoformat(),
        }
        assert facilities["B No Doctors"]["next_available"] is None

    def test_matches_the_facility_endpoint(self, client, make, day, at, freeze):
        freeze(at(16, 50))
        facility = make.facility()
        make.doctor(facility, hours=(time(8), time(17)), name="Dr. A")
        make.doctor(facility, weekdays=[(day + timedelta(days=2)).weekday()])

        [listed] = client.get("/facilities").json()

        assert listed["next_available"] == ok(client, facility)["next_available"]

    def test_query_count_does_not_grow_with_facilities(
        self, client, make, count_queries
    ):
        make.doctor(make.facility(name="First"))
        with count_queries() as one:
            client.get("/facilities")

        for i in range(4):
            facility = make.facility(name=f"More {i}")
            make.doctor(facility)
            make.doctor(facility)
        with count_queries() as five:
            client.get("/facilities")

        assert len(five) == len(one)
