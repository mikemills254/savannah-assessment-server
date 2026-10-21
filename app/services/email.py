"""Transactional email: invites and password resets.

Each message is sent as a background task after the response, so a slow or
failing mail server never delays or fails the request. A failure is logged
(with the user's id, never their email) and the user can ask again: an admin
can reissue an invite, and anyone can request another reset link.
"""

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

from app.core.config import settings
from app.core.permissions import UserRole

logger = logging.getLogger(__name__)

TEMPLATE_FOLDER = Path(__file__).resolve().parent.parent / "templates" / "email"

config = ConnectionConfig(
    MAIL_USERNAME=settings.mail_username,
    MAIL_PASSWORD=settings.mail_password,
    MAIL_FROM=settings.mail_from,
    MAIL_FROM_NAME=settings.mail_from_name,
    MAIL_PORT=settings.mail_port,
    MAIL_SERVER=settings.mail_server,
    # Port 465 is implicit TLS; anything else (usually 587) upgrades with STARTTLS.
    MAIL_STARTTLS=settings.mail_port != 465,
    MAIL_SSL_TLS=settings.mail_port == 465,
    USE_CREDENTIALS=True,
    VALIDATE_CERTS=True,
    TEMPLATE_FOLDER=TEMPLATE_FOLDER,
)


def _client_url(path: str, token: str) -> str:
    return f"{settings.frontend_url.rstrip('/')}{path}?token={quote(token, safe='')}"


def invite_url(token: str) -> str:
    return _client_url("/auth/accept-invite", token)


def reset_url(token: str) -> str:
    return _client_url("/auth/reset-password", token)


async def deliver(message: MessageSchema, template_name: str) -> None:
    """The one place mail leaves the app; the tests replace it."""
    await FastMail(config).send_message(message, template_name=template_name)


async def _send(
    *,
    user_id: UUID,
    kind: str,
    to: str,
    subject: str,
    template: str,
    body: dict[str, Any],
) -> None:
    message = MessageSchema(
        subject=subject,
        recipients=[to],
        template_body=body,
        subtype=MessageType.html,
    )
    extra = {"event": f"email.{kind}", "user_id": str(user_id)}
    try:
        await deliver(message, template)
    except Exception:
        logger.exception("Couldn't send email", extra={**extra, "outcome": "failed"})
        return
    logger.info("Email sent", extra={**extra, "outcome": "sent"})


_ROLE_LINE = {
    UserRole.DOCTOR: "as a doctor",
    UserRole.FACILITY_ADMIN: "as an admin",
    UserRole.PATIENT: "as a patient",
}


async def send_invite(
    *,
    user_id: UUID,
    email: str,
    name: str,
    role: UserRole,
    facility_name: str,
    token: str,
    appointment: dict[str, str] | None = None,
) -> None:
    """Invite a new account to set its password. A patient booked in by
    the clinic also sees the visit (`appointment`: doctor, when)."""
    subject = (
        f"Your appointment at {facility_name}"
        if appointment
        else f"You're invited to join {facility_name}"
    )
    await _send(
        user_id=user_id,
        kind="invite",
        to=email,
        subject=subject,
        template="invite.html",
        body={
            "name": name,
            "role": role.value,
            "role_line": _ROLE_LINE.get(role, ""),
            "facility_name": facility_name,
            "invite_url": invite_url(token),
            "expires_in_hours": settings.invite_token_expire_hours,
            "appointment": appointment,
        },
    )


async def send_added_to_facility(
    *, user_id: UUID, email: str, name: str, facility_id: UUID, facility_name: str
) -> None:
    """Tell a doctor who already has an account that another facility added
    them, and link to setting their hours there."""
    await _send(
        user_id=user_id,
        kind="added_to_facility",
        to=email,
        subject=f"{facility_name} added you as a doctor",
        template="added_to_facility.html",
        body={
            "name": name,
            "facility_name": facility_name,
            "hours_url": (
                f"{settings.frontend_url.rstrip('/')}/doctor/hours"
                f"?facility={facility_id}"
            ),
        },
    )


async def send_password_reset(
    *, user_id: UUID, email: str, name: str, token: str
) -> None:
    await _send(
        user_id=user_id,
        kind="password_reset",
        to=email,
        subject="Reset your DocConnect password",
        template="password_reset.html",
        body={
            "name": name,
            "reset_url": reset_url(token),
            "expires_in_minutes": settings.password_reset_expire_minutes,
        },
    )
