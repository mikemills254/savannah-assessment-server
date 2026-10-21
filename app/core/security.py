import hashlib
import hmac
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
RESET_TOKEN_TYPE = "password_reset"


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hasher.verify(password, hashed_password)


def _create_token(
    user_id: UUID, token_type: str, expires_in: timedelta, **claims: str
) -> str:
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "exp": datetime.now(UTC) + expires_in,
        **claims,
    }
    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def _decode_claims(token: str, token_type: str) -> tuple[UUID, datetime] | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != token_type:
            return None
        return UUID(payload["sub"]), datetime.fromtimestamp(payload["exp"], UTC)
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        return None


def _decode_token(token: str, token_type: str) -> UUID | None:
    claims = _decode_claims(token, token_type)
    return claims[0] if claims else None


def create_access_token(user_id: UUID) -> str:
    return _create_token(
        user_id,
        ACCESS_TOKEN_TYPE,
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def decode_access_token(token: str) -> UUID | None:
    """Return the user id from a valid token, or None if it is invalid or expired."""
    return _decode_token(token, ACCESS_TOKEN_TYPE)


def access_token_expires_at(token: str) -> datetime | None:
    """When a valid access token expires, or None if it is invalid or expired."""
    claims = _decode_claims(token, ACCESS_TOKEN_TYPE)
    return claims[1] if claims else None


def create_invite_token(user_id: UUID) -> str:
    return _create_token(
        user_id,
        INVITE_TOKEN_TYPE,
        timedelta(hours=settings.invite_token_expire_hours),
    )


def decode_invite_token(token: str) -> UUID | None:
    return _decode_token(token, INVITE_TOKEN_TYPE)


def password_fingerprint(hashed_password: str | None) -> str:
    """A short digest of the user's current password hash. A reset token
    carries it, so the token stops working once the password changes: it
    can be used once, and an older link dies when a newer one is used."""
    return hashlib.sha256((hashed_password or "").encode()).hexdigest()[:16]


def create_password_reset_token(user_id: UUID, hashed_password: str | None) -> str:
    return _create_token(
        user_id,
        RESET_TOKEN_TYPE,
        timedelta(minutes=settings.password_reset_expire_minutes),
        pwd=password_fingerprint(hashed_password),
    )


def decode_password_reset_token(token: str) -> tuple[UUID, str] | None:
    """The user id and password fingerprint, or None if the token is invalid
    or expired. The caller compares the fingerprint with the user's."""
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        if payload.get("type") != RESET_TOKEN_TYPE:
            return None
        return UUID(payload["sub"]), str(payload["pwd"])
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        return None


def fingerprint_matches(expected: str, hashed_password: str | None) -> bool:
    return hmac.compare_digest(expected, password_fingerprint(hashed_password))
