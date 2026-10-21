"""Live appointment events: who is told what, and the event stream."""

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from redis.client import PubSub

from app.core.redis import redis, redis_sync
from app.core.security import ACCESS_TOKEN_TYPE, _create_token
from app.services.events import (
    channels_for,
    facility_channel,
    stream,
    user_channel,
)
from tests.conftest import auth


@pytest.fixture
def listen(empty_redis) -> Iterator:
    """listen(*channels) subscribes and returns a function that collects
    what has been published to them since."""
    subscriptions: list[PubSub] = []

    def _listen(*channels: str):
        pubsub = redis_sync.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(*channels)
        subscriptions.append(pubsub)

        def received() -> list[tuple[str, dict]]:
            messages = []
            while (message := pubsub.get_message(timeout=0.2)) is not None or (
                # Subscribe confirmations come back as None too; keep reading
                # while anything is buffered.
                pubsub.connection is not None and pubsub.connection.can_read()
            ):
                if message is not None:
                    messages.append((message["channel"], json.loads(message["data"])))
            return messages

        return received

    yield _listen
    for pubsub in subscriptions:
        pubsub.close()


def _book(client, patient, doctor, starts_at):
    return client.post(
        "/appointments",
        json={"doctor_id": str(doctor.id), "starts_at": starts_at.isoformat()},
        headers=auth(patient),
    )


class TestPublishing:
    def test_a_booking_tells_its_patient_doctor_and_facility(
        self, client, make, at, listen
    ):
        facility = make.facility()
        doctor = make.doctor(facility)
        patient = make.patient(name="Jane Doe")
        received = listen(
            user_channel(patient.id),
            user_channel(doctor.id),
            facility_channel(facility.id),
        )

        response = _book(client, patient, doctor, at(9))

        assert response.status_code == 201, response.text
        messages = received()
        assert sorted(channel for channel, _ in messages) == sorted(
            [
                user_channel(patient.id),
                user_channel(doctor.id),
                facility_channel(facility.id),
            ]
        )
        for _, event in messages:
            assert event["type"] == "appointment.booked"
            assert event["actor_id"] == str(patient.id)
            # The same appointment the booking response returned.
            assert event["appointment"] == response.json()

    def test_a_booking_for_a_patient_names_the_admin(self, client, make, at, listen):
        facility = make.facility()
        doctor = make.doctor(facility)
        admin = make.admin(facility)
        received = listen(user_channel(doctor.id))

        response = client.post(
            f"/facilities/{facility.id}/appointments",
            json={
                "doctor_id": str(doctor.id),
                "starts_at": at(9).isoformat(),
                "patient": {"email": "walk-in@example.com", "name": "Walk In"},
            },
            headers=auth(admin),
        )

        assert response.status_code == 201, response.text
        [(_, event)] = received()
        assert event["type"] == "appointment.booked"
        assert event["actor_id"] == str(admin.id)
        assert event["appointment"]["patient"]["name"] == "Walk In"

    def test_other_doctors_and_facilities_are_not_told(self, client, make, at, listen):
        facility = make.facility()
        doctor = make.doctor(facility)
        other_facility = make.facility(name="Elsewhere")
        other_doctor = make.doctor(other_facility)
        received = listen(
            user_channel(other_doctor.id), facility_channel(other_facility.id)
        )

        _book(client, make.patient(), doctor, at(9))

        assert received() == []

    def test_a_cancellation_and_a_reschedule_are_published(
        self, client, make, at, listen
    ):
        facility = make.facility()
        doctor = make.doctor(facility)
        patient = make.patient()
        booking = _book(client, patient, doctor, at(9)).json()
        received = listen(user_channel(patient.id))

        client.patch(
            f"/appointments/{booking['id']}/reschedule",
            json={"starts_at": at(10).isoformat()},
            headers=auth(doctor),
        )
        client.patch(
            f"/appointments/{booking['id']}/cancel",
            json={"reason": "Doctor unavailable"},
            headers=auth(doctor),
        )

        [(_, moved), (_, cancelled)] = received()
        assert moved["type"] == "appointment.rescheduled"
        assert moved["actor_id"] == str(doctor.id)
        assert moved["appointment"]["starts_at"] == at(10).isoformat()
        assert cancelled["type"] == "appointment.cancelled"
        assert cancelled["appointment"]["status"] == "CANCELLED"
        assert cancelled["appointment"]["cancellation_reason"] == "Doctor unavailable"

    def test_nothing_is_published_when_nothing_changes(self, client, make, at, listen):
        facility = make.facility()
        doctor = make.doctor(facility)
        patient = make.patient()
        headers = {**auth(patient), "Idempotency-Key": "booking-1"}
        body = {"doctor_id": str(doctor.id), "starts_at": at(9).isoformat()}
        booking = client.post("/appointments", json=body, headers=headers).json()
        received = listen(user_channel(doctor.id))

        replay = client.post("/appointments", json=body, headers=headers)
        same_time = client.patch(
            f"/appointments/{booking['id']}/reschedule",
            json={"starts_at": at(9).isoformat()},
            headers=auth(patient),
        )
        taken = _book(client, make.patient(), doctor, at(9))

        assert replay.headers["Idempotent-Replayed"] == "true"
        assert same_time.status_code == 200
        assert taken.status_code == 409
        assert received() == []

    def test_a_booking_still_succeeds_when_redis_is_down(
        self, client, make, at, monkeypatch, caplog
    ):
        from redis.exceptions import ConnectionError as RedisConnectionError

        def down(*args, **kwargs):
            raise RedisConnectionError("down")

        facility = make.facility()
        doctor = make.doctor(facility)
        patient = make.patient()
        # Only the publish fails; the rate limiter's Redis calls still work.
        monkeypatch.setattr(redis_sync, "pipeline", down)

        response = _book(client, patient, doctor, at(9))

        assert response.status_code == 201, response.text
        assert any(
            getattr(r, "event", "") == "events.publish_failed" for r in caplog.records
        )


class TestChannels:
    def test_each_role_follows_what_it_can_read(self, make):
        facility = make.facility()
        patient = make.patient()
        doctor = make.doctor(facility)
        admin = make.admin(facility)

        assert channels_for(patient) == [user_channel(patient.id)]
        assert channels_for(doctor) == [user_channel(doctor.id)]
        assert channels_for(admin) == [facility_channel(facility.id)]


class TestStreamRoute:
    def test_needs_a_token(self, client):
        assert client.get("/events").status_code == 401

    def test_is_refused_to_super_admins(self, client, make):
        from app.core.permissions import UserRole

        super_admin = make._user(UserRole.SUPER_ADMIN)

        response = client.get("/events", headers=auth(super_admin))

        assert response.status_code == 403

    def test_streams_the_callers_events_until_the_token_expires(
        self, client, make, empty_redis
    ):
        doctor = make.doctor(make.facility())
        # Expires shortly, so the stream ends and the response can be read
        # whole.
        token = _create_token(doctor.id, ACCESS_TOKEN_TYPE, timedelta(seconds=1.5))
        payload = json.dumps({"type": "appointment.booked", "appointment": {}})
        publish = threading.Timer(
            0.5, redis_sync.publish, args=(user_channel(doctor.id), payload)
        )
        publish.start()
        try:
            response = client.get(
                "/events", headers={"Authorization": f"Bearer {token}"}
            )
        finally:
            publish.cancel()

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.text.startswith("retry: 3000\nevent: ready\ndata: {}\n\n")
        assert f"event: appointment\ndata: {payload}\n\n" in response.text


@pytest.mark.anyio
class TestStream:
    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"

    @pytest.fixture(autouse=True)
    async def fresh_connections(self, empty_redis):
        # The async client's pooled connections belong to the event loop that
        # made them; each test has its own loop.
        await redis.connection_pool.disconnect()
        yield
        await redis.connection_pool.disconnect()

    async def test_ends_at_the_deadline(self):
        frames = [
            frame
            async for frame in stream(
                ["events:test"], until=datetime.now(UTC) + timedelta(seconds=0.2)
            )
        ]

        assert frames == ["retry: 3000\nevent: ready\ndata: {}\n\n"]

    async def test_sends_a_heartbeat_when_idle(self):
        frames = stream(
            ["events:test"],
            until=datetime.now(UTC) + timedelta(seconds=5),
            heartbeat=0.1,
        )
        try:
            assert "event: ready" in await anext(frames)
            assert await anext(frames) == ": ping\n\n"
        finally:
            await frames.aclose()
