"""Test setup: a dedicated Postgres database built by the real migrations.

The partial unique indexes that stop double booking only exist in Postgres,
so the booking tests run against it rather than SQLite or mocks.

The database is TEST_DATABASE_URL, or DATABASE_URL with "_test" appended to
its name. It must end in "_test": the concurrency tests commit and then
truncate every table.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from dotenv import dotenv_values
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
_dotenv = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}


def _env(key: str) -> str | None:
    return os.environ.get(key) or _dotenv.get(key)


def _test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        url = make_url(explicit)
    else:
        base = _env("DATABASE_URL")
        if not base:
            raise RuntimeError("Set TEST_DATABASE_URL or DATABASE_URL to run the tests")
        url = make_url(base)
        url = url.set(database=f"{url.database}_test")
    if not (url.database or "").endswith("_test"):
        raise RuntimeError(
            f"Refusing to run tests against {url.database!r}: the name must end in _test"
        )
    return url.render_as_string(hide_password=False)


# Set before anything imports app.core.config, which reads them once.
os.environ["DATABASE_URL"] = _test_database_url()
os.environ["REDIS_URL"] = _env("REDIS_URL") or "redis://localhost:6379/0"
os.environ["JWT_SECRET_KEY"] = "test-secret-key-with-at-least-32-bytes!"
# Placeholders: no test sends email.
os.environ["MAIL_SERVER"] = "smtp.invalid"
os.environ["MAIL_USERNAME"] = "test"
os.environ["MAIL_PASSWORD"] = "test"
os.environ["MAIL_FROM"] = "no-reply@example.com"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.permissions import UserRole  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.dependencies.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import DoctorWorkingHour, Facility, User  # noqa: E402

TZ_NAME = "Africa/Nairobi"
TZ = ZoneInfo(TZ_NAME)


def _create_database_if_missing(url_string: str) -> None:
    url = make_url(url_string)
    server = create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", future=True
    )
    with server.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": url.database},
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    server.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    _create_database_if_missing(os.environ["DATABASE_URL"])
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

    from app.db.session import engine as app_engine

    yield app_engine
    app_engine.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """A session whose commits are savepoints inside one outer transaction
    that is rolled back after the test, so tests can't see each other."""
    with engine.connect() as conn:
        outer = conn.begin()
        session = Session(
            bind=conn,
            join_transaction_mode="create_savepoint",
            autoflush=False,
            expire_on_commit=False,
        )
        try:
            yield session
        finally:
            session.close()
            outer.rollback()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    # Not used as a context manager, so the lifespan's db/Redis startup pings
    # don't run; requests use the test's session.
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@dataclass
class Factory:
    """Creates the rows a test needs and commits them (a savepoint under the
    db fixture)."""

    db: Session

    def facility(self, **kwargs) -> Facility:
        facility = Facility(
            name=kwargs.pop("name", "Test Clinic"),
            timezone=kwargs.pop("timezone", TZ_NAME),
            **kwargs,
        )
        self.db.add(facility)
        self.db.commit()
        return facility

    def doctor(
        self,
        facility: Facility,
        *,
        hours: tuple[time, time] = (time(8), time(17)),
        weekdays: range = range(7),
        **kwargs,
    ) -> User:
        doctor = self._user(UserRole.DOCTOR, facility=facility, **kwargs)
        self.db.add_all(
            DoctorWorkingHour(
                doctor_id=doctor.id, weekday=d, start_time=hours[0], end_time=hours[1]
            )
            for d in weekdays
        )
        self.db.commit()
        return doctor

    def patient(self, **kwargs) -> User:
        return self._user(UserRole.PATIENT, **kwargs)

    def admin(self, facility: Facility, **kwargs) -> User:
        return self._user(UserRole.FACILITY_ADMIN, facility=facility, **kwargs)

    def _user(self, role: UserRole, **kwargs) -> User:
        user = User(
            email=kwargs.pop(
                "email", f"{role.value.lower()}-{uuid4().hex[:8]}@example.com"
            ),
            name=kwargs.pop("name", role.value.title()),
            role=role,
            **kwargs,
        )
        self.db.add(user)
        self.db.commit()
        return user


@pytest.fixture
def make(db: Session) -> Factory:
    return Factory(db)


@pytest.fixture
def day() -> date:
    """A clinic-local date a week ahead: always in the future, and every test
    doctor works every weekday."""
    return datetime.now(TZ).date() + timedelta(days=7)


@pytest.fixture
def at(day: date):
    """at(9) or at(9, 30): that time on `day` in the clinic's timezone."""

    def _at(hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=TZ)

    return _at
