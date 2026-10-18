import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
import uvicorn

from app.core.errors import AppError
from app.db.session import engine
from app.routes import auth, facilities

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast on startup instead of on the first request that touches the db.
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        logger.error("Could not connect to database at %s: %s", engine.url, exc.orig)
        raise
    logger.info("Database connection OK (%s)", engine.url)
    yield
    engine.dispose()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    # Same {"detail": ...} shape as FastAPI's own HTTPException responses.
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(auth.router)
app.include_router(facilities.router)


@app.get("/")
async def read_root():
    return {
        "status": "ok",
        "message": "Hello, World!"
    }


if __name__ == "__main__":
    # reload only works when uvicorn is given an import string, not the app object.
    uvicorn.run("app.main:app", host="0.0.0.0", reload=True, port=8000)
