import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
import uvicorn

from app.core.errors import AppError
from app.core.redis import redis
from app.db.session import engine
from app.routes import appointments, auth, doctors, facilities, patients

logger = logging.getLogger("uvicorn.error")


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
    engine.dispose()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    # Same {"detail": ...} shape as FastAPI's own HTTPException responses.
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(auth.router)
app.include_router(facilities.router)
app.include_router(doctors.router)
app.include_router(appointments.router)
app.include_router(patients.router)


@app.get("/")
async def read_root():
    return {
        "status": "ok",
        "message": "Hello, World!"
    }


@app.get("/health")
async def health() -> JSONResponse:
    """Readiness: 503 while the db or Redis can't be reached. The Docker
    healthcheck calls this."""
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
        content={"status": "ok" if healthy else "unavailable", **checks},
    )


if __name__ == "__main__":
    # reload only works when uvicorn is given an import string, not the app object.
    uvicorn.run("app.main:app", host="0.0.0.0", reload=True, port=8000)
