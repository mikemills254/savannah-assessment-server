"""Two requests racing for the same slot, on separate connections.

These commit for real (the per-test rollback can't be shared across
connections), so they clean up by truncating the test database's tables.
"""

import threading
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Appointment, User
from app.services.appointments import AppointmentService, SlotTakenError
from tests.conftest import Factory


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE appointments, idempotency_keys, doctor_working_hours,"
                " users, facilities CASCADE"
            )
        )


def race(sessions: sessionmaker[Session], *calls: Callable[[Session], object]) -> list:
    """Run each call in its own thread and session, released together.
    Returns each call's result or the exception it raised, in order."""
    barrier = threading.Barrier(len(calls))
    results: list = [None] * len(calls)

    def run(i: int, call: Callable[[Session], object]) -> None:
        with sessions() as db:
            barrier.wait()
            try:
                results[i] = call(db)
            except Exception as exc:
                # Returned for the test to check.
                results[i] = exc

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_only_one_of_two_simultaneous_bookings_wins(sessions, at):
    with sessions() as setup:
        make = Factory(setup)
        doctor = make.doctor(make.facility())
        alice, bob = make.patient(), make.patient()

    def book(patient):
        return lambda db: AppointmentService(db).book(
            db.get(User, patient.id), doctor_id=doctor.id, starts_at=at(9)
        )

    results = race(sessions, book(alice), book(bob))

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1, results
    assert isinstance(errors[0], SlotTakenError), results
    with sessions() as db:
        count = db.scalar(
            select(func.count()).where(Appointment.doctor_id == doctor.id)
        )
    assert count == 1


def test_a_double_submitted_booking_is_stored_once(sessions, at):
    with sessions() as setup:
        make = Factory(setup)
        doctor = make.doctor(make.facility())
        patient = make.patient()

    def submit(db):
        return AppointmentService(db).book(
            db.get(User, patient.id),
            doctor_id=doctor.id,
            starts_at=at(9),
            idempotency_key="double-click",
        )

    results = race(sessions, submit, submit)

    assert not any(isinstance(r, Exception) for r in results), results
    (first, first_replayed), (second, second_replayed) = results
    assert first.id == second.id
    assert sorted([first_replayed, second_replayed]) == [False, True]
