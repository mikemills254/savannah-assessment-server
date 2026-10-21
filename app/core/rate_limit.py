"""Rate limiting in Redis, with escalating blocks.

A policy allows `limit` requests per `window` seconds (a sliding window) for
one identity: a client IP, a user, or an IP and email together. Going over
blocks that identity, for 1 minute the first time, then 5, 15, and at most
30 minutes each further time, until it goes `strike_ttl` without being
blocked. A request while blocked gets 429 with Retry-After, and doesn't
extend the block.

Each check is one Lua script, so it's atomic across every API instance
sharing the Redis, and it reads Redis's clock, not each instance's. If Redis
can't be reached, requests are let through: the limiter protects the API, it
shouldn't take it down.
"""

import hashlib
import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request, status
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.errors import AppError
from app.core.redis import redis_sync
from app.core.security import decode_access_token

logger = logging.getLogger(__name__)

MINUTE = 60


@dataclass(frozen=True)
class Policy:
    name: str
    limit: int
    window: int  # seconds
    # Block lengths for the 1st, 2nd, ... time over; the last one repeats.
    penalties: tuple[int, ...] = (1 * MINUTE, 5 * MINUTE, 15 * MINUTE, 30 * MINUTE)
    # How long being blocked is remembered, so the next block is longer.
    strike_ttl: int = 60 * MINUTE


# Every API route, per signed-in user, or per IP when signed out.
API = Policy("api", limit=300, window=MINUTE)
# Password guessing against one account from one IP.
LOGIN = Policy("login", limit=5, window=MINUTE)
# One IP trying many accounts.
LOGIN_IP = Policy("login-ip", limit=20, window=MINUTE)
REGISTER = Policy("register", limit=5, window=10 * MINUTE)
GOOGLE_LOGIN = Policy("google-login", limit=10, window=MINUTE)
# Guessing invite tokens.
ACCEPT_INVITE = Policy("accept-invite", limit=5, window=MINUTE)
# Reset emails: per IP, and per address so nobody's inbox can be flooded.
FORGOT_PASSWORD = Policy("forgot-password", limit=5, window=10 * MINUTE)
FORGOT_PASSWORD_EMAIL = Policy("forgot-password-email", limit=3, window=15 * MINUTE)
# Guessing reset tokens.
RESET_PASSWORD = Policy("reset-password", limit=5, window=MINUTE)


class TooManyRequestsError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        self.headers = {"Retry-After": str(retry_after)}
        minutes = math.ceil(retry_after / MINUTE)
        wait = f"{minutes} minutes" if minutes > 1 else "a minute"
        super().__init__(f"Too many requests. Try again in {wait}.")


# KEYS: block, hits (sorted set of request times), strikes.
# ARGV: limit, window ms, strike ttl ms, request id, penalties ms...
# Returns {allowed, remaining, retry after ms, strikes if this call blocked}.
_SCRIPT = redis_sync.register_script(
    """
local blocked = redis.call('PTTL', KEYS[1])
if blocked > 0 then
  return {0, 0, blocked, 0}
end

local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[2])
if count < limit then
  redis.call('ZADD', KEYS[2], now, ARGV[4])
  redis.call('PEXPIRE', KEYS[2], window)
  return {1, limit - count - 1, 0, 0}
end

local strikes = redis.call('INCR', KEYS[3])
redis.call('PEXPIRE', KEYS[3], tonumber(ARGV[3]))
local penalty = tonumber(ARGV[4 + math.min(strikes, #ARGV - 4)])
redis.call('SET', KEYS[1], '1', 'PX', penalty)
-- A fresh window once the block ends.
redis.call('DEL', KEYS[2])
return {0, 0, penalty, strikes}
"""
)


def keys(policy: Policy, identity: str) -> list[str]:
    """The policy's Redis keys for an identity. Hashed, so no email or IP is
    stored in Redis; the {hash tag} keeps all three in one Redis Cluster slot."""
    digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
    base = f"rl:{{{policy.name}:{digest}}}"
    return [f"{base}:block", f"{base}:hits", f"{base}:strikes"]


def check(policy: Policy, identity: str) -> None:
    """Count a request by `identity` against the policy. Raises
    TooManyRequestsError if it's over the limit or already blocked."""
    if not settings.rate_limit_enabled:
        return
    try:
        allowed, _remaining, retry_ms, strikes = _SCRIPT(
            keys=keys(policy, identity),
            args=[
                policy.limit,
                policy.window * 1000,
                policy.strike_ttl * 1000,
                uuid.uuid4().hex,
                *(p * 1000 for p in policy.penalties),
            ],
        )
    except RedisError:
        logger.warning(
            "Rate limiter unavailable; allowing the request",
            exc_info=True,
            extra={"event": "rate_limit.unavailable", "policy": policy.name},
        )
        return

    if allowed:
        return
    retry_after = math.ceil(retry_ms / 1000)
    if strikes:
        logger.warning(
            "Rate limit exceeded; blocked for %ss",
            retry_after,
            extra={
                "event": "rate_limit.blocked",
                "policy": policy.name,
                "strikes": strikes,
                "retry_after": retry_after,
            },
        )
    raise TooManyRequestsError(retry_after)


def client_ip(request: Request) -> str:
    # uvicorn's --proxy-headers sets this from the proxy's X-Forwarded-For.
    return request.client.host if request.client else "unknown"


def caller(request: Request) -> str:
    """The signed-in user if the request has a valid token, else its IP, so
    staff sharing one clinic IP each get their own allowance."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        user_id = decode_access_token(token)
        if user_id is not None:
            return f"user:{user_id}"
    return f"ip:{client_ip(request)}"


def rate_limit(
    policy: Policy, key: Callable[[Request], str] = caller
) -> Callable[[Request], None]:
    """A dependency that applies the policy: Depends(rate_limit(LOGIN_IP, client_ip))."""

    def dependency(request: Request) -> None:
        check(policy, key(request))

    return dependency
