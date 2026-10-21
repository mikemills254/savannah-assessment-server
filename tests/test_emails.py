"""Password reset and invite emails: what is sent, to whom, with which link,
and that each link does what it says (once)."""

import logging
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.config import settings
from app.core.security import (
    create_invite_token,
    create_password_reset_token,
    hash_password,
)
from tests.conftest import auth

PASSWORD = "a-long-password"
NEW_PASSWORD = "a-brand-new-password"
# Hashed once: argon2 is slow on purpose.
HASHED = hash_password(PASSWORD)


def link_in(email) -> tuple[str, str]:
    """The path and token of the email's action link."""
    url = email.body.get("reset_url") or email.body["invite_url"]
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == settings.frontend_url
    assert url in email.html
    return parts.path, parse_qs(parts.query)["token"][0]


def forgot(client, email: str):
    return client.post("/auth/forgot-password", json={"email": email})


def reset(client, token: str, password: str = NEW_PASSWORD):
    return client.post(
        "/auth/reset-password", json={"token": token, "password": password}
    )


def login(client, email: str, password: str):
    return client.post("/auth/login", json={"email": email, "password": password})


class TestForgotPassword:
    def test_emails_a_reset_link_to_the_account(self, client, make, outbox):
        patient = make.patient(name="Pat", hashed_password=HASHED)

        response = forgot(client, patient.email.upper())

        assert response.status_code == 202, response.text
        [email] = outbox
        assert email.to == patient.email
        assert email.template == "password_reset.html"
        assert "Pat" in email.html
        path, _ = link_in(email)
        assert path == "/auth/reset-password"

    def test_an_unknown_email_gets_the_same_answer_and_no_email(
        self, client, make, outbox
    ):
        known = forgot(client, make.patient(hashed_password=HASHED).email)
        outbox.clear()

        unknown = forgot(client, "nobody@example.com")

        assert unknown.status_code == known.status_code == 202
        assert unknown.json() == known.json()
        assert outbox == []

    def test_a_deactivated_account_gets_no_email(self, client, make, outbox):
        patient = make.patient(hashed_password=HASHED, is_active=False)

        assert forgot(client, patient.email).status_code == 202
        assert outbox == []

    def test_one_address_can_only_be_sent_a_few(self, client, make, outbox):
        patient = make.patient(hashed_password=HASHED)

        statuses = [forgot(client, patient.email).status_code for _ in range(4)]

        assert statuses == [202, 202, 202, 429]
        assert len(outbox) == 3


class TestResetPassword:
    def test_sets_the_password_and_signs_in(self, client, make, outbox):
        patient = make.patient(hashed_password=HASHED)
        forgot(client, patient.email)
        _, token = link_in(outbox[0])

        response = reset(client, token)

        assert response.status_code == 200, response.text
        assert response.json()["user"]["id"] == str(patient.id)
        assert response.json()["access_token"]
        assert login(client, patient.email, NEW_PASSWORD).status_code == 200
        assert login(client, patient.email, PASSWORD).status_code == 401

    def test_a_link_works_once(self, client, make, outbox):
        patient = make.patient(hashed_password=HASHED)
        forgot(client, patient.email)
        _, token = link_in(outbox[0])

        assert reset(client, token).status_code == 200
        second = reset(client, token, "yet-another-password")

        assert second.status_code == 400
        assert "already used" in second.json()["detail"]

    def test_using_a_newer_link_cancels_an_older_one(self, client, make, outbox):
        patient = make.patient(hashed_password=HASHED)
        forgot(client, patient.email)
        forgot(client, patient.email)
        (_, older), (_, newer) = link_in(outbox[0]), link_in(outbox[1])

        assert reset(client, newer).status_code == 200
        assert reset(client, older).status_code == 400

    def test_an_expired_link_is_refused(self, client, make, monkeypatch):
        patient = make.patient(hashed_password=HASHED)
        monkeypatch.setattr(settings, "password_reset_expire_minutes", -1)
        token = create_password_reset_token(patient.id, patient.hashed_password)

        assert reset(client, token).status_code == 400

    def test_an_invite_token_is_not_a_reset_token(self, client, make):
        patient = make.patient(hashed_password=HASHED)

        assert reset(client, create_invite_token(patient.id)).status_code == 400

    def test_a_deactivated_account_cannot_reset(self, client, db, make):
        patient = make.patient(hashed_password=HASHED)
        token = create_password_reset_token(patient.id, patient.hashed_password)
        patient.is_active = False
        db.commit()

        assert reset(client, token).status_code == 400

    def test_an_invited_user_can_set_a_password_this_way(self, client, make, outbox):
        doctor = make.doctor(make.facility())  # invited, no password yet
        forgot(client, doctor.email)
        _, token = link_in(outbox[0])

        assert reset(client, token).status_code == 200
        assert login(client, doctor.email, NEW_PASSWORD).status_code == 200

    def test_a_weak_password_is_refused(self, client, make, outbox):
        patient = make.patient(hashed_password=HASHED)
        forgot(client, patient.email)
        _, token = link_in(outbox[0])

        assert reset(client, token, "short").status_code == 422


@pytest.mark.parametrize("role", ["DOCTOR", "FACILITY_ADMIN"])
class TestStaffInvites:
    def test_a_new_member_is_emailed_a_working_invite(self, client, make, outbox, role):
        facility = make.facility(name="St. Jude")
        admin = make.admin(facility)

        response = client.post(
            f"/facilities/{facility.id}/members",
            json={"email": "new@example.com", "role": role, "name": "Wanjiru"},
            headers=auth(admin),
        )

        assert response.status_code == 201, response.text
        [email] = outbox
        assert email.to == "new@example.com"
        assert email.subject == "You're invited to join St. Jude"
        path, token = link_in(email)
        # The client's page, not /accept-invite, which it doesn't have.
        assert path == "/auth/accept-invite"
        assert token == response.json()["invite_token"]
        accepted = client.post(
            "/auth/accept-invite", json={"token": token, "password": PASSWORD}
        )
        assert accepted.status_code == 200, accepted.text

    def test_a_reissued_invite_is_emailed(self, client, make, outbox, role):
        facility = make.facility()
        admin = make.admin(facility)
        member = make.doctor(facility) if role == "DOCTOR" else make.admin(facility)

        response = client.post(
            f"/facilities/{facility.id}/members/{member.id}/invite",
            headers=auth(admin),
        )

        assert response.status_code == 200, response.text
        [email] = outbox
        assert email.to == member.email
        assert link_in(email)[1] == response.json()["invite_token"]


class TestPatientInvites:
    def book(self, client, admin, facility, doctor, starts_at, key=None):
        headers = auth(admin) | ({"Idempotency-Key": key} if key else {})
        return client.post(
            f"/facilities/{facility.id}/appointments",
            json={
                "doctor_id": str(doctor.id),
                "starts_at": starts_at.isoformat(),
                "patient": {"email": "walk-in@example.com", "name": "Walk In"},
            },
            headers=headers,
        )

    def test_a_new_patient_is_emailed_their_appointment(self, client, make, at, outbox):
        facility = make.facility(name="St. Jude")
        doctor = make.doctor(facility, name="Dr. Achieng Otieno")
        admin = make.admin(facility)

        response = self.book(client, admin, facility, doctor, at(10, 30))

        assert response.status_code == 201, response.text
        [email] = outbox
        assert email.to == "walk-in@example.com"
        assert email.subject == "Your appointment at St. Jude"
        appointment = email.body["appointment"]
        assert appointment["doctor"] == "Dr. Achieng Otieno"
        # In the clinic's local time, as the patient will see it there.
        assert appointment["when"].endswith(", 10:30")
        assert str(at(10, 30).day) in appointment["when"]
        assert appointment["when"] in email.html
        assert link_in(email)[1] == response.json()["invite_token"]

    def test_a_replayed_booking_sends_no_second_email(self, client, make, at, outbox):
        facility = make.facility()
        doctor = make.doctor(facility)
        admin = make.admin(facility)

        first = self.book(client, admin, facility, doctor, at(9), key="walk-in-1")
        again = self.book(client, admin, facility, doctor, at(9), key="walk-in-1")

        assert first.status_code == 201
        assert again.headers["Idempotent-Replayed"] == "true"
        assert len(outbox) == 1

    def test_a_patient_who_can_sign_in_gets_no_invite(self, client, make, at, outbox):
        facility = make.facility()
        doctor = make.doctor(facility)
        admin = make.admin(facility)
        make.patient(email="walk-in@example.com", hashed_password=HASHED)

        response = self.book(client, admin, facility, doctor, at(9))

        assert response.status_code == 201
        assert response.json()["invite_token"] is None
        assert outbox == []

    def test_a_reissued_patient_invite_is_emailed(self, client, make, at, outbox):
        facility = make.facility()
        doctor = make.doctor(facility)
        admin = make.admin(facility)
        booked = self.book(client, admin, facility, doctor, at(9))
        patient_id = booked.json()["appointment"]["patient"]["id"]
        outbox.clear()

        response = client.post(
            f"/facilities/{facility.id}/patients/{patient_id}/invite",
            headers=auth(admin),
        )

        assert response.status_code == 200, response.text
        [email] = outbox
        assert email.to == "walk-in@example.com"
        assert email.body["appointment"] is None


class TestDeliveryFailure:
    def test_is_logged_without_the_address_and_the_request_succeeds(
        self, client, make, monkeypatch, caplog
    ):
        from app.services import email

        async def down(*args, **kwargs):
            raise ConnectionError("SMTP is down")

        monkeypatch.setattr(email, "deliver", down)
        patient = make.patient(hashed_password=HASHED)

        with caplog.at_level(logging.ERROR, logger="app.services.email"):
            response = forgot(client, patient.email)

        assert response.status_code == 202
        [record] = [r for r in caplog.records if r.name == "app.services.email"]
        assert record.user_id == str(patient.id)
        assert patient.email not in caplog.text
