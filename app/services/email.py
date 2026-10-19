import logging
from pathlib import Path
from urllib.parse import quote

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

from app.core.config import settings

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


def invite_url(token: str) -> str:
    return f"{settings.frontend_url}/accept-invite?token={quote(token, safe='')}"


async def send_doctor_invite(
    *, email: str, name: str, facility_name: str, token: str
) -> None:
    """Email a doctor their invite link. Runs as a background task, so a
    failure is logged instead of raised: the admin still has the token from
    the API response to share by hand."""
    message = MessageSchema(
        subject=f"You're invited to join {facility_name}",
        recipients=[email],
        template_body={
            "name": name,
            "facility_name": facility_name,
            "invite_url": invite_url(token),
            "expires_in_hours": settings.invite_token_expire_hours,
        },
        subtype=MessageType.html,
    )
    try:
        await FastMail(config).send_message(message, template_name="doctor_invite.html")
    except Exception:
        logger.exception("Failed to send doctor invite to %s", email)
