from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from pwdlib import PasswordHash

from app.core.config import settings

password_hasher = PasswordHash.recommended()

# Stored in the token's "type" claim so an invite link can't be used as a
# login token, or the other way round.
ACCESS_TOKEN_TYPE = "access"
INVITE_TOKEN_TYPE = "invite"


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hasher.verify(password, hashed_password)


def _create_token(user_id: UUID, token_type: str, expires_in: timedelta) -> str:
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "exp": datetime.now(UTC) + expires_in,
    }
    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def _decode_token(token: str, token_type: str) -> UUID | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != token_type:
            return None
        return UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None


def create_access_token(user_id: UUID) -> str:
    return _create_token(
        user_id,
        ACCESS_TOKEN_TYPE,
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def decode_access_token(token: str) -> UUID | None:
    """Return the user id from a valid token, or None if it is invalid or expired."""
    return _decode_token(token, ACCESS_TOKEN_TYPE)


def create_invite_token(user_id: UUID) -> str:
    return _create_token(
        user_id,
        INVITE_TOKEN_TYPE,
        timedelta(hours=settings.invite_token_expire_hours),
    )


def decode_invite_token(token: str) -> UUID | None:
    return _decode_token(token, INVITE_TOKEN_TYPE)
