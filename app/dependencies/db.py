from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import SessionLocal


def get_db() -> Generator[Session]:
    """One session per request, closed automatically when the request ends."""
    with SessionLocal() as db:
        yield db


DbSession = Annotated[Session, Depends(get_db)]
