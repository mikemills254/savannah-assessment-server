"""Live appointment updates, pushed to browsers as Server-Sent Events.

When a booking, cancellation or reschedule commits, the appointment is
published to Redis pub/sub on one channel per audience: its patient's, its
doctor's and its facility's (the facility admins'). Each open event stream
subscribes to its user's channel, so an event reaches the browser whichever
API instance it is connected to.

Delivery is best effort. If Redis is down the booking still succeeds and the
event is lost, so clients don't rely on seeing every event: they reload their
data whenever their stream (re)connects.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

import anyio
from redis.exceptions import RedisError

from app.core.permissions import UserRole
from app.core.redis import redis, redis_sync
from app.models.appointment import Appointment
from app.models.user import User
from app.schemas.appointment import AppointmentRead

logger = logging.getLogger(__name__)

# A comment line this often keeps proxies from closing an idle stream, and
# shows the server a client that has gone away.
HEARTBEAT_SECONDS = 15.0
# How long a browser's EventSource waits before reconnecting.
RETRY_MS = 3000


class AppointmentEvent(str, Enum):
    BOOKED = "appointment.booked"
    CANCELLED = "appointment.cancelled"
    RESCHEDULED = "appointment.rescheduled"


def user_channel(user_id: UUID) -> str:
    return f"events:user:{user_id}"


def facility_channel(facility_id: UUID) -> str:
    return f"events:facility:{facility_id}"


def channels_for(user: User) -> list[str]:
    """What the user is shown live: the same appointments they can read.
    Patients and doctors get their own; facility admins all of their
    facility's. Super admins don't see bookings, so they get none."""
    if user.role == UserRole.FACILITY_ADMIN and user.facility_id is not None:
        return [facility_channel(user.facility_id)]
    if user.role in (UserRole.PATIENT, UserRole.DOCTOR):
        return [user_channel(user.id)]
    return []


def publish_appointment_event(
    event: AppointmentEvent, appointment: Appointment, actor: User
) -> None:
    """Tell the appointment's patient, doctor and facility admins. Call it
    after the change commits, with the appointment's details loaded."""
    message = json.dumps(
        {
            "type": event.value,
            # So the browser of whoever made the change can skip announcing
            # it back to them.
            "actor_id": str(actor.id),
            "appointment": AppointmentRead.model_validate(appointment).model_dump(
                mode="json"
            ),
        }
    )
    channels = (
        user_channel(appointment.patient_id),
        user_channel(appointment.doctor_id),
        facility_channel(appointment.facility_id),
    )
    try:
        pipe = redis_sync.pipeline(transaction=False)
        for channel in channels:
            pipe.publish(channel, message)
        pipe.execute()
    except RedisError:
        # The change is committed; only the live update is lost.
        logger.warning(
            "Couldn't publish appointment event",
            exc_info=True,
            extra={
                "event": "events.publish_failed",
                "type": event.value,
                "appointment_id": str(appointment.id),
            },
        )


def _frame(event: str, data: str) -> str:
    # data is one line of JSON, so it needs one data: field.
    return f"event: {event}\ndata: {data}\n\n"


async def stream(
    channels: list[str],
    *,
    until: datetime,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Server-Sent Events for the channels' messages, until `until` (the
    caller's token expiry). The client then reconnects with its current
    token.

    Starts with a "ready" event once subscribed. Everything published after
    it is delivered, so a client that reloads its data on "ready" misses
    nothing. If Redis fails the stream ends, and the client reconnects."""
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(*channels)
        yield f"retry: {RETRY_MS}\n" + _frame("ready", "{}")

        last_sent = time.monotonic()
        while (remaining := (until - datetime.now(UTC)).total_seconds()) > 0:
            wait = min(heartbeat - (time.monotonic() - last_sent), remaining)
            message = await pubsub.get_message(timeout=max(wait, 0))
            if message is not None:
                yield _frame("appointment", message["data"])
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= heartbeat:
                yield ": ping\n\n"
                last_sent = time.monotonic()
    except RedisError:
        logger.warning(
            "Event stream lost Redis; closing it",
            exc_info=True,
            extra={"event": "events.stream_failed"},
        )
    finally:
        # A client disconnecting cancels this; shielded so the connection
        # still goes back to the pool.
        with anyio.CancelScope(shield=True):
            await pubsub.aclose()
