from fastapi import APIRouter, status
from fastapi.responses import StreamingResponse

from app.core.errors import ForbiddenError
from app.core.security import access_token_expires_at
from app.dependencies.auth import Credentials, StreamingUser
from app.services.events import channels_for, stream

router = APIRouter(tags=["events"])


@router.get(
    "/events",
    response_class=StreamingResponse,
    responses={
        status.HTTP_200_OK: {
            "content": {"text/event-stream": {}},
            "description": "A Server-Sent Events stream",
        }
    },
)
async def stream_events(user: StreamingUser, credentials: Credentials):
    """Live updates for the caller, as Server-Sent Events. Send the token in
    the Authorization header, as for any other route (so read the stream
    with fetch, not EventSource), never in the URL.

    - `ready`: subscribed. Reload your data now; every change after this
      is delivered.
    - `appointment`: `{type, actor_id, appointment}` when an appointment you
      can see is booked, cancelled or rescheduled. type is
      appointment.booked, appointment.cancelled or appointment.rescheduled;
      appointment is as GET /appointments/{id} returns it. Patients and
      doctors get their own appointments; facility admins all of their
      facility's.
    - Comment lines are heartbeats.

    The stream ends when the token expires; reconnect with a current one.
    403 for super admins, who don't see bookings."""
    channels = channels_for(user)
    if not channels:
        raise ForbiddenError("This account has no live updates")
    # get_streaming_user has accepted the token, so it has an expiry.
    assert credentials is not None
    until = access_token_expires_at(credentials.credentials)
    assert until is not None

    return StreamingResponse(
        stream(channels, until=until),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Stops nginx-style proxies holding events back in a buffer.
            "X-Accel-Buffering": "no",
        },
    )
