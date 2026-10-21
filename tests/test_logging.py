"""Request ids, the access log, and the log line formats."""

import json
import logging
import re
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.logging import (
    ConsoleFormatter,
    JsonFormatter,
    RequestContextMiddleware,
    configure_logging,
)
from app.main import API_PREFIX
from tests.conftest import ROOT_URL, auth


def records(caplog, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == name]


class TestRequestId:
    def test_each_response_carries_a_new_one(self, client):
        first = client.get(f"{ROOT_URL}/").headers["X-Request-ID"]
        second = client.get(f"{ROOT_URL}/").headers["X-Request-ID"]

        assert re.fullmatch(r"[0-9a-f]{32}", first)
        assert first != second

    def test_the_callers_id_is_kept(self, client):
        response = client.get(f"{ROOT_URL}/", headers={"X-Request-ID": "web-1a2b.3c_4"})

        assert response.headers["X-Request-ID"] == "web-1a2b.3c_4"

    @pytest.mark.parametrize("bad", ["has spaces", "x" * 65, "new\\nline", ""])
    def test_a_malformed_one_is_replaced(self, client, bad):
        response = client.get(f"{ROOT_URL}/", headers={"X-Request-ID": bad})

        assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


class TestAccessLog:
    def test_one_line_per_request_with_its_ids_and_no_query_string(
        self, client, make, caplog
    ):
        caplog.set_level(logging.INFO)
        patient = make.patient()

        response = client.get("/auth/me?search=Jane+Doe", headers=auth(patient))

        [line] = records(caplog, "app.access")
        assert line.levelno == logging.INFO
        assert (line.method, line.path, line.status) == (
            "GET",
            f"{API_PREFIX}/auth/me",
            200,
        )
        assert line.duration_ms >= 0
        assert line.client_ip == "testclient"
        assert line.context == {
            "request_id": response.headers["X-Request-ID"],
            "user_id": str(patient.id),
        }
        assert "Jane" not in line.getMessage()

    def test_lines_logged_while_handling_a_request_carry_its_ids(
        self, client, make, caplog, at
    ):
        caplog.set_level(logging.INFO)
        facility = make.facility()
        doctor = make.doctor(facility)
        patient = make.patient()

        response = client.post(
            "/appointments",
            json={"doctor_id": str(doctor.id), "starts_at": at(9).isoformat()},
            headers=auth(patient),
        )

        assert response.status_code == 201, response.text
        [booked] = [
            r for r in caplog.records if getattr(r, "event", "") == "appointment.booked"
        ]
        assert booked.appointment_id == response.json()["id"]
        assert booked.doctor_id == str(doctor.id)
        assert booked.context == {
            "request_id": response.headers["X-Request-ID"],
            "user_id": str(patient.id),
        }

    def test_a_failed_login_is_logged_without_the_email(self, client, caplog):
        caplog.set_level(logging.INFO)

        client.post(
            "/auth/login",
            json={"email": "someone@example.com", "password": "wrong-password"},
        )

        [failed] = [
            r for r in caplog.records if getattr(r, "event", "") == "auth.login_failed"
        ]
        assert "someone" not in json.dumps(vars(failed), default=str)


class TestUnhandledError:
    @pytest.fixture
    def api(self) -> TestClient:
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)

        @app.get("/boom")
        def boom() -> None:
            raise RuntimeError("boom")

        return TestClient(app)

    def test_is_a_500_with_the_request_id(self, api):
        response = api.get("/boom")

        assert response.status_code == 500
        assert response.json() == {
            "detail": "Internal server error",
            "request_id": response.headers["X-Request-ID"],
        }

    def test_is_logged_with_its_traceback_and_an_error_access_line(self, api, caplog):
        response = api.get("/boom")

        [error] = records(caplog, "app.core.logging")
        assert error.exc_info[0] is RuntimeError
        assert error.context["request_id"] == response.headers["X-Request-ID"]
        [line] = records(caplog, "app.access")
        assert (line.levelno, line.status) == (logging.ERROR, 500)


def _record(**extra) -> logging.LogRecord:
    try:
        raise ValueError("bad value")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.getLogger("app.test").makeRecord(
        "app.test",
        logging.ERROR,
        __file__,
        1,
        "Failed for %s",
        ("x",),
        exc_info,
        extra=extra,
    )
    record.context = {"request_id": "r1", "user_id": "u1"}
    return record


class TestFormat:
    def test_json_is_one_object_per_line_with_context_extras_and_traceback(self):
        line = JsonFormatter().format(_record(event="thing.failed", count=2))

        assert "\n" not in line
        entry = json.loads(line)
        assert entry["level"] == "ERROR"
        assert entry["logger"] == "app.test"
        assert entry["message"] == "Failed for x"
        assert (entry["request_id"], entry["user_id"]) == ("r1", "u1")
        assert (entry["event"], entry["count"]) == ("thing.failed", 2)
        assert "ValueError: bad value" in entry["exception"]
        assert re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", entry["time"]
        )

    def test_console_is_readable_with_the_fields_after_the_message(self):
        text = ConsoleFormatter().format(_record(event="thing.failed"))

        first, *rest = text.splitlines()
        assert "ERROR" in first
        assert "app.test: Failed for x" in first
        assert "request_id=r1" in first
        assert "event=thing.failed" in first
        assert "ValueError: bad value" in "\n".join(rest)

    def test_configuring_again_keeps_one_handler(self):
        configure_logging()
        configure_logging()

        ours = [
            h for h in logging.getLogger().handlers if getattr(h, "is_app_handler", 0)
        ]
        assert len(ours) == 1
