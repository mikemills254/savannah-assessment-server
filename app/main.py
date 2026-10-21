import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import RequestContextMiddleware, configure_logging
from app.core.rate_limit import API, rate_limit
from app.core.redis import redis, redis_sync
from app.db.session import engine
from app.routes import appointments, auth, doctors, events, facilities, patients

configure_logging()
logger = logging.getLogger(__name__)

# Every API route is under this; / and /health are not.
API_PREFIX = "/api/v1"


def ping_db() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast on startup instead of on the first request that touches the db
    # or Redis.
    try:
        ping_db()
    except OperationalError as exc:
        logger.error("Could not connect to database at %s: %s", engine.url, exc.orig)
        raise
    logger.info("Database connection OK (%s)", engine.url)
    try:
        await redis.ping()
    except RedisError as exc:
        logger.error("Could not connect to Redis: %s", exc)
        raise
    logger.info("Redis connection OK")
    yield
    await redis.aclose()
    redis_sync.close()
    engine.dispose()


app = FastAPI(lifespan=lifespan)

# Inside CORS, so the 500 it returns for an unhandled error still has the CORS
# headers the browser needs to read it.
app.add_middleware(RequestContextMiddleware)

# The deployed web client is on another origin. Auth is a bearer token, not
# cookies, so credentials aren't allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    expose_headers=["Idempotent-Replayed", "Retry-After", "X-Request-ID"],
    max_age=600,
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    # Same {"detail": ...} shape as FastAPI's own HTTPException responses.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


# Every API route shares one allowance per caller; / and /health don't count.
# The auth routes add tighter limits of their own. An event stream counts
# once, when it connects.
for router in (auth, facilities, doctors, appointments, patients, events):
    app.include_router(
        router.router,
        prefix=API_PREFIX,
        dependencies=[Depends(rate_limit(API))],
    )


@app.get("/")
async def read_root():
    return {"status": "ok", "message": "Hello, World!"}


@app.get("/health")
async def health() -> JSONResponse:
    """Readiness: 503 while the db or Redis can't be reached. The Docker
    healthcheck and the host's health check call this; the deploy pipeline
    reads `commit` to confirm the new version is live."""
    checks = {"database": "ok", "redis": "ok"}
    try:
        await run_in_threadpool(ping_db)
    except SQLAlchemyError:
        checks["database"] = "unavailable"
    try:
        await redis.ping()
    except RedisError:
        checks["redis"] = "unavailable"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "unavailable",
            **checks,
            "commit": settings.git_commit,
        },
    )


if __name__ == "__main__":
    # reload only works when uvicorn is given an import string, not the app object.
    # Open event streams never finish on their own; the timeout stops them
    # holding up a reload.
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        reload=True,
        port=8000,
        timeout_graceful_shutdown=2,
    )
