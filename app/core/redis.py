from redis import Redis as SyncRedis
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry as SyncRetry

from app.core.config import settings

redis = Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_timeout=5.0,
    socket_connect_timeout=5.0,
    # A command that hits a dropped connection or a timeout is retried 3 times,
    # backing off from 50ms up to 1s, then raises. Kept short: a request is
    # waiting on it.
    retry=Retry(ExponentialBackoff(cap=1.0, base=0.05), retries=3),
    retry_on_error=[ConnectionError, TimeoutError],
    # Pings a connection idle this long before reusing it, so one the server
    # dropped is replaced instead of failing the next command.
    health_check_interval=30,
)

# For code that runs in sync routes and dependencies, i.e. in the threadpool,
# such as the rate limiter. Tighter timeouts and one retry: it runs on every
# request, and when Redis is down the limiter lets requests through rather
# than holding each one up for seconds.
redis_sync = SyncRedis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_timeout=1.0,
    socket_connect_timeout=1.0,
    retry=SyncRetry(ExponentialBackoff(cap=0.2, base=0.05), retries=1),
    retry_on_error=[ConnectionError, TimeoutError],
    health_check_interval=30,
)
