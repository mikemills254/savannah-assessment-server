from dataclasses import dataclass

from google.auth import exceptions as google_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.core.config import settings

# Reuses one HTTP session for fetching Google's signing keys.
_request = google_requests.Request()


@dataclass(frozen=True)
class GoogleIdentity:
    # Google's stable account id; unlike the email, it never changes.
    sub: str
    email: str
    name: str | None


def google_sign_in_enabled() -> bool:
    return settings.google_client_id is not None


def verify_google_id_token(token: str) -> GoogleIdentity | None:
    """Return the identity in a Google ID token issued to our client id, or
    None if the token is invalid, expired or its email is unverified."""
    try:
        claims = id_token.verify_oauth2_token(
            token,
            _request,
            settings.google_client_id,
            clock_skew_in_seconds=10,
        )
    except (ValueError, google_exceptions.GoogleAuthError):
        return None

    # Only a verified email proves the person owns it, which is what lets us
    # link the Google account to an existing account with that email.
    if not claims.get("email") or not claims.get("email_verified"):
        return None

    return GoogleIdentity(
        sub=claims["sub"],
        email=claims["email"],
        name=claims.get("name"),
    )
