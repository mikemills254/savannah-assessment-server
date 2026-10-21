"""Test setup: a dedicated Postgres database built by the real migrations.

The partial unique indexes that stop double booking only exist in Postgres,
so the booking tests run against it rather than SQLite or mocks.

The database is TEST_DATABASE_URL, or DATABASE_URL with "_test" appended to
its name. It must end in "_test": the concurrency tests commit and then
truncate every table.

Redis is REDIS_URL's server, but database 15, which is emptied before every
test that makes requests, so rate limits don't carry from one test to the next.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from dotenv import dotenv_values
from sqlalchemy import Engine, create_engine, event, make_url, text
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


TEST_REDIS_DB = 15


def _test_redis_url() -> str:
    url = urlsplit(_env("REDIS_URL") or "redis://localhost:6379/0")
    return urlunsplit(url._replace(path=f"/{TEST_REDIS_DB}"))


# Set before anything imports app.core.config, which reads them once.
os.environ["DATABASE_URL"] = _test_database_url()
os.environ["REDIS_URL"] = _test_redis_url()
os.environ["JWT_SECRET_KEY"] = "test-secret-key-with-at-least-32-bytes!"
# Placeholders: no test sends email (the outbox fixture catches it).
os.environ["MAIL_SERVER"] = "smtp.invalid"
os.environ["MAIL_USERNAME"] = "test"
os.environ["MAIL_PASSWORD"] = "test"
os.environ["MAIL_FROM"] = "no-reply@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:5173"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.permissions import UserRole  # noqa: E402
from app.core.redis import redis_sync  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.dependencies.db import get_db  # noqa: E402
from app.main import API_PREFIX, app  # noqa: E402
from app.models import DoctorFacility, DoctorWorkingHour, Facility, User  # noqa: E402

ROOT_URL = "http://testserver"
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
def count_queries(engine: Engine):
    """with count_queries() as statements: collects the SQL run inside it.
    Call db.expunge_all() first so nothing is already loaded, as in a real
    request's new session."""

    @contextmanager
    def counting() -> Iterator[list[str]]:
        statements: list[str] = []

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            yield statements
        finally:
            event.remove(engine, "before_cursor_execute", record)

    return counting


@pytest.fixture
def empty_redis() -> None:
    assert redis_sync.connection_pool.connection_kwargs["db"] == TEST_REDIS_DB
    redis_sync.flushdb()


@pytest.fixture
def client(db: Session, empty_redis: None) -> Iterator[TestClient]:
    """Paths are relative to the API prefix: client.get("/auth/me") requests
    /api/v1/auth/me. Routes outside it need the full URL, ROOT_URL + "/health"."""
    # Not used as a context manager, so the lifespan's db/Redis startup pings
    # don't run; requests use the test's session.
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app, base_url=f"{ROOT_URL}{API_PREFIX}")
    finally:
        app.dependency_overrides.clear()


@dataclass
class SentEmail:
    to: str
    subject: str
    template: str
    body: dict
    html: str


@pytest.fixture(autouse=True)
def outbox(monkeypatch) -> list[SentEmail]:
    """Every email sent during the test, rendered with its real template
    (so a broken template fails the test) but never delivered."""
    from app.services import email

    sent: list[SentEmail] = []

    async def capture(message, template_name: str) -> None:
        body = dict(message.template_body)
        template = email.config.template_engine().get_template(template_name)
        sent.append(
            SentEmail(
                to=str(message.recipients[0].email),
                subject=message.subject,
                template=template_name,
                body=body,
                html=template.render(**body),
            )
        )

    monkeypatch.setattr(email, "deliver", capture)
    return sent


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
        doctor = self._user(UserRole.DOCTOR, **kwargs)
        self.link(doctor, facility, hours=hours, weekdays=weekdays)
        return doctor

    def link(
        self,
        doctor: User,
        facility: Facility,
        *,
        hours: tuple[time, time] | None = (time(8), time(17)),
        weekdays: range = range(7),
        is_active: bool = True,
    ) -> DoctorFacility:
        """The doctor working at the facility too, with these hours there
        (None for none)."""
        link = DoctorFacility(
            doctor_id=doctor.id, facility_id=facility.id, is_active=is_active
        )
        self.db.add(link)
        self.db.flush()
        if hours is not None:
            self.db.add_all(
                DoctorWorkingHour(
                    doctor_id=doctor.id,
                    facility_id=facility.id,
                    weekday=d,
                    start_time=hours[0],
                    end_time=hours[1],
                )
                for d in weekdays
            )
        self.db.commit()
        return link

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


def next_slot_start() -> datetime:
    """The first half-hour boundary after now in the clinic's time: always
    under an hour away, so too soon to book."""
    now = datetime.now(TZ)
    start = now.replace(minute=0, second=0, microsecond=0)
    while start <= now:
        start += timedelta(minutes=30)
    return start


@pytest.fixture
def at(day: date):
    """at(9) or at(9, 30): that time on `day` in the clinic's timezone."""

    def _at(hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=TZ)

    return _at
