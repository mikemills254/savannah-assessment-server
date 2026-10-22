"""Sign-up roles (the first account is the super admin), and the signed-in
user (MeRead) as the auth routes return it: facilities."""

import pytest
from sqlalchemy import func, select

from app.core.google import GoogleIdentity
from app.core.permissions import UserRole
from app.core.security import create_invite_token, hash_password
from app.models import Facility, User
from app.schemas.facility import FacilityRead
from app.services.auth import AuthService
from tests.conftest import auth

PASSWORD = "a-long-password"
# Hashed once: argon2 is slow on purpose.
HASHED = hash_password(PASSWORD)


def listed(facility: Facility) -> list[dict]:
    """facilities as the API should return them for staff of `facility`."""
    return [FacilityRead.model_validate(facility).model_dump(mode="json")]


def login(client, user) -> dict:
    response = client.post(
        "/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


def me(client, user) -> dict:
    response = client.get("/auth/me", headers=auth(user))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def staff(make):
    """staff("admin" | "doctor", facility): a member who can log in."""

    def _staff(role: str, facility: Facility):
        add = make.admin if role == "admin" else make.doctor
        return add(facility, hashed_password=HASHED)

    return _staff


def register(client, email: str, **extra) -> dict:
    return client.post(
        "/auth/register",
        json={"name": "Someone", "email": email, "password": PASSWORD, **extra},
    )


class TestFirstAccount:
    def test_the_first_sign_up_becomes_the_super_admin(self, client):
        first = register(client, "owner@example.com")
        second = register(client, "pat@example.com")

        assert first.status_code == 201, first.text
        assert first.json()["user"]["role"] == "SUPER_ADMIN"
        assert second.status_code == 201, second.text
        assert second.json()["user"]["role"] == "PATIENT"

    def test_the_first_sign_up_can_not_register_a_facility(self, client, db):
        response = register(
            client,
            "ada@example.com",
            facility_name="Riverside Clinic",
            timezone="Africa/Nairobi",
        )

        assert response.status_code == 400, response.text
        assert db.scalar(select(func.count()).select_from(User)) == 0
        assert db.scalar(select(func.count()).select_from(Facility)) == 0

    def test_the_first_google_sign_in_becomes_the_super_admin(self, db):
        service = AuthService(db)

        first = service.login_with_google(
            GoogleIdentity(sub="g-1", email="owner@example.com", name="Owner")
        )
        second = service.login_with_google(
            GoogleIdentity(sub="g-2", email="pat@example.com", name="Pat")
        )

        assert first.role == UserRole.SUPER_ADMIN
        assert second.role == UserRole.PATIENT


class TestRegister:
    @pytest.fixture(autouse=True)
    def _not_the_first_account(self, make):
        make.patient()

    def test_a_clinic_sign_up_lists_the_new_facility(self, client, db):
        response = client.post(
            "/auth/register",
            json={
                "name": "Ada Admin",
                "email": "ada@example.com",
                "password": PASSWORD,
                "facility_name": "Riverside Clinic",
                "timezone": "Africa/Nairobi",
            },
        )

        assert response.status_code == 201, response.text
        user = response.json()["user"]
        facility = db.get(Facility, user["facility_id"])
        assert facility.name == "Riverside Clinic"
        assert user["facilities"] == listed(facility)

    def test_a_patient_sign_up_lists_none(self, client):
        response = client.post(
            "/auth/register",
            json={"name": "Pat", "email": "pat@example.com", "password": PASSWORD},
        )

        assert response.status_code == 201, response.text
        assert response.json()["user"]["role"] == "PATIENT"
        assert response.json()["user"]["facilities"] == []


@pytest.mark.parametrize("role", ["admin", "doctor"])
class TestStaff:
    def test_login_and_me_list_their_facility(self, client, make, staff, role):
        facility = make.facility(name="Hillside", timezone="Europe/London")
        member = staff(role, facility)

        for user in (login(client, member), me(client, member)):
            [listed_facility] = user["facilities"]
            assert listed_facility["id"] == str(facility.id)
            # Only an admin has a facility_id; a doctor can work at several,
            # so theirs are only listed.
            assert user["facility_id"] == (
                str(facility.id) if role == "admin" else None
            )
            assert listed_facility["name"] == "Hillside"
            assert listed_facility["timezone"] == "Europe/London"
            assert listed_facility["is_active"] is True
            assert user["facilities"] == listed(facility)

    def test_an_inactive_facility_is_still_listed(self, client, make, staff, role):
        facility = make.facility(is_active=False)
        member = staff(role, facility)

        for user in (login(client, member), me(client, member)):
            assert user["facilities"] == listed(facility)
            assert user["facilities"][0]["is_active"] is False


class TestOthers:
    def test_a_patient_lists_none(self, client, make):
        patient = make.patient(hashed_password=HASHED)

        assert login(client, patient)["facilities"] == []
        assert me(client, patient)["facilities"] == []

    def test_a_super_admin_lists_none(self, client, make):
        from app.core.permissions import UserRole

        super_admin = make._user(UserRole.SUPER_ADMIN)

        assert me(client, super_admin)["facilities"] == []


class TestAcceptInvite:
    def test_an_invited_doctor_gets_their_facility(self, client, make):
        facility = make.facility()
        doctor = make.doctor(facility)  # no password yet

        response = client.post(
            "/auth/accept-invite",
            json={"token": create_invite_token(doctor.id), "password": PASSWORD},
        )

        assert response.status_code == 200, response.text
        assert response.json()["user"]["facilities"] == listed(facility)


def queries(statements: list[str]) -> list[str]:
    # Without the SAVEPOINTs the test's db session wraps each transaction in.
    return [s for s in statements if "SAVEPOINT" not in s]


class TestQueries:
    def test_me_loads_the_facility_with_the_user(
        self, client, db, make, staff, count_queries
    ):
        doctor = staff("doctor", make.facility())
        # As in a real request's new session, nothing is already loaded.
        db.expunge_all()

        with count_queries() as statements:
            assert len(me(client, doctor)["facilities"]) == 1

        [statement] = queries(statements)
        assert "JOIN facilities" in statement

    def test_login_loads_the_facility_with_the_user(
        self, client, db, make, staff, count_queries
    ):
        admin = staff("admin", make.facility())
        db.expunge_all()

        with count_queries() as statements:
            assert len(login(client, admin)["facilities"]) == 1

        [statement] = queries(statements)
        assert "JOIN facilities" in statement
